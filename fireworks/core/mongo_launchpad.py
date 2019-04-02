# coding: utf-8

from __future__ import unicode_literals

from monty.io import zopen
from monty.os.path import zpath

"""
The LaunchPad manages the FireWorks database.
"""

import datetime
import json
import os
import random
import time
import traceback
import shutil
import gridfs
from collections import OrderedDict, defaultdict
from itertools import chain
from tqdm import tqdm
from bson import ObjectId

from pymongo import MongoClient
from pymongo import DESCENDING, ASCENDING
from pymongo.errors import DocumentTooLarge
from monty.serialization import loadfn

from fireworks.fw_config import LAUNCHPAD_LOC, SORT_FWS, RESERVATION_EXPIRATION_SECS, \
    RUN_EXPIRATION_SECS, MAINTAIN_INTERVAL, WFLOCK_EXPIRATION_SECS, WFLOCK_EXPIRATION_KILL, \
    MONGO_SOCKET_TIMEOUT_MS, GRIDFS_FALLBACK_COLLECTION
from fireworks.utilities.fw_serializers import FWSerializable, reconstitute_dates
from fireworks.core.firework import Firework, Launch, Workflow, FWAction, Tracker
from fireworks.utilities.fw_utilities import get_fw_logger

__author__ = 'Anubhav Jain'
__copyright__ = 'Copyright 2013, The Materials Project'
__version__ = '0.1'
__maintainer__ = 'Anubhav Jain'
__email__ = 'ajain@lbl.gov'
__date__ = 'Jan 30, 2013'

# TODO: lots of duplication reduction and cleanup possible


class LockedWorkflowError(ValueError):
    """
    Error raised if the context manager WFLock can't acquire the lock on the WF within the selected
    time interval (WFLOCK_EXPIRATION_SECS), if the killing of the lock is disabled (WFLOCK_EXPIRATION_KILL)
    """
    pass


class WFLock(object):
    """
    Lock a Workflow, i.e. for performing update operations
    Raises a LockedWorkflowError if the lock couldn't be acquired withing expire_secs and kill==False.
    Calling functions are responsible for handling the error in order to avoid database inconsistencies.
    """

    def __init__(self, lp, fw_id, expire_secs=WFLOCK_EXPIRATION_SECS, kill=WFLOCK_EXPIRATION_KILL):
        """
        Args:
            lp (LaunchPad)
            fw_id (int): Firework id
            expire_secs (int): max waiting time in seconds.
            kill (bool): force lock acquisition or not
        """
        self.lp = lp
        self.fw_id = fw_id
        self.expire_secs = expire_secs
        self.kill = kill

    def __enter__(self):
        ctr = 0
        waiting_time = 0
        # acquire lock
        links_dict = self.lp.workflows.find_one_and_update({'nodes': self.fw_id,
                                                            'locked': {"$exists": False}},
                                                           {'$set': {'locked': True}})
        # could not acquire lock b/c WF is already locked for writing
        while not links_dict:
            ctr += 1
            time_incr = ctr/10.0+random.random()/100.0
            time.sleep(time_incr)  # wait a bit for lock to free up
            waiting_time += time_incr
            if waiting_time > self.expire_secs:  # too much time waiting, expire lock
                wf = self.lp.workflows.find_one({'nodes': self.fw_id})
                if not wf:
                    raise ValueError("Could not find workflow in database: {}".format(self.fw_id))
                if self.kill:  # force lock acquisition
                    self.lp.m_logger.warning('FORCIBLY ACQUIRING LOCK, WF: {}'.format(self.fw_id))
                    links_dict = self.lp.workflows.find_one_and_update({'nodes': self.fw_id},
                                                                       {'$set': {'locked': True}})
                else:  # throw error if we don't want to force lock acquisition
                    raise LockedWorkflowError("Could not get workflow - LOCKED: {}".format(self.fw_id))
            else:
                # retry lock
                links_dict = self.lp.workflows.find_one_and_update(
                    {'nodes': self.fw_id, 'locked': {"$exists": False}}, {'$set': {'locked': True}})

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.lp.workflows.find_one_and_update({"nodes": self.fw_id}, {"$unset": {"locked": True}})


class MongoLaunchPad(FWSerializable):
    """
    The LaunchPad manages the FireWorks database.
    """

    def __init__(self, host='localhost', port=27017, name='fireworks', username=None, password=None,
                 logdir=None, strm_lvl=None, user_indices=None, wf_user_indices=None, ssl=False,
                 ssl_ca_certs=None, ssl_certfile=None, ssl_keyfile=None, ssl_pem_passphrase=None,
                 authsource=None):
        """
        Args:
            host (str): hostname
            port (int): port number
            name (str): database name
            username (str)
            password (str)
            logdir (str): path to the log directory
            strm_lvl (str): the logger stream level
            user_indices (list): list of 'fireworks' collection indexes to be built
            wf_user_indices (list): list of 'workflows' collection indexes to be built
            ssl (bool): use TLS/SSL for mongodb connection
            ssl_ca_certs (str): path to the CA certificate to be used for mongodb connection
            ssl_certfile (str): path to the client certificate to be used for mongodb connection
            ssl_keyfile (str): path to the client private key
            ssl_pem_passphrase (str): passphrase for the client private key
            authsource (str): authsource parameter for MongoDB authentication; defaults to "name" (i.e., db name) if not set
        """
        self.host = host
        self.port = port
        self.name = name
        self.username = username
        self.password = password
        self.ssl = ssl
        self.ssl_ca_certs = ssl_ca_certs
        self.ssl_certfile = ssl_certfile
        self.ssl_keyfile = ssl_keyfile
        self.ssl_pem_passphrase = ssl_pem_passphrase
        self.authsource = authsource or name

        # set up logger
        self.logdir = logdir
        self.strm_lvl = strm_lvl if strm_lvl else 'INFO'
        self.m_logger = get_fw_logger('launchpad', l_dir=self.logdir, stream_level=self.strm_lvl)

        self.user_indices = user_indices if user_indices else []
        self.wf_user_indices = wf_user_indices if wf_user_indices else []

        # get connection
        self.connection = MongoClient(host, port, ssl=self.ssl,
            ssl_ca_certs=self.ssl_ca_certs, ssl_certfile=self.ssl_certfile,
            ssl_keyfile=self.ssl_keyfile, ssl_pem_passphrase=self.ssl_pem_passphrase,
            socketTimeoutMS=MONGO_SOCKET_TIMEOUT_MS, username=username, password=password,
            authSource=self.authsource)
        self.db = self.connection[name]

        self.fireworks = self.db.fireworks
        self.fw_id_assigner = self.db.fw_id_assigner
        self.workflows = self.db.workflows
        if GRIDFS_FALLBACK_COLLECTION:
            self.gridfs_fallback = gridfs.GridFS(self.db, GRIDFS_FALLBACK_COLLECTION)
        else:
            self.gridfs_fallback = None

        self.backup_launch_data = {}
        self.backup_fw_data = {}

    def to_dict(self):
        """
        Note: usernames/passwords are exported as unencrypted Strings!
        """
        return {
            'host': self.host,
            'port': self.port,
            'name': self.name,
            'username': self.username,
            'password': self.password,
            'logdir': self.logdir,
            'strm_lvl': self.strm_lvl,
            'user_indices': self.user_indices,
            'wf_user_indices': self.wf_user_indices,
            'ssl': self.ssl,
            'ssl_ca_certs': self.ssl_ca_certs,
            'ssl_certfile': self.ssl_certfile,
            'ssl_keyfile': self.ssl_keyfile,
            'ssl_pem_passphrase': self.ssl_pem_passphrase,
            'authsource': self.authsource}

    def update_spec(self, fw_ids, spec_document, mongo=False):
        """#
        Update fireworks with a spec. Sometimes you need to modify a firework in progress.

        Args:
            fw_ids [int]: All fw_ids to modify.
            spec_document (dict): The spec document. Note that only modifications to
                the spec key are allowed. So if you supply {"_tasks.1.parameter": "hello"},
                you are effectively modifying spec._tasks.1.parameter in the actual fireworks
                collection.
            mongo (bool): spec_document uses mongo syntax to directly update the spec
        """
        # Might want to decide whether to edit all fws or just the most recent ones,
        # in order to preserve spec history? Maybe not, since this isn't a current
        # feature anyways.
        if mongo:
            mod_spec = spec_document
        else:
            mod_spec = {"$set": {("spec." + k): v for k, v in spec_document.items()} }

        allowed_states = ["READY", "WAITING", "FIZZLED", "DEFUSED", "PAUSED"]
        self.fireworks.update_many({'fw_id': {"$in": fw_ids},
                                    'state': {"$in": allowed_states}}, mod_spec)
        for fw in self.fireworks.find({'fw_id': {"$in": fw_ids}, 'state': {"$nin": allowed_states}},
                                      {"fw_id": 1, "state": 1}):
            self.m_logger.warning("Cannot update spec of fw_id: {} with state: {}. "
                               "Try rerunning first".format(fw['fw_id'], fw['state']))

    @classmethod
    def from_dict(cls, d):
        logdir = d.get('logdir', None)
        strm_lvl = d.get('strm_lvl', None)
        user_indices = d.get('user_indices', [])
        wf_user_indices = d.get('wf_user_indices', [])
        ssl = d.get('ssl', False)
        ssl_ca_certs = d.get('ssl_ca_certs', d.get('ssl_ca_file', None))  # ssl_ca_file was the old notation for FWS < 1.5.5
        ssl_certfile = d.get('ssl_certfile', None)
        ssl_keyfile = d.get('ssl_keyfile', None)
        ssl_pem_passphrase = d.get('ssl_pem_passphrase', None)
        authsource= d.get('authsource', None)
        return LaunchPad(d['host'], d['port'], d['name'], d['username'], d['password'],
                         logdir, strm_lvl, user_indices, wf_user_indices, ssl,
                         ssl_ca_certs, ssl_certfile, ssl_keyfile, ssl_pem_passphrase,
                         authsource)

    def _reset(self):
        self.fireworks.delete_many({})
        self.workflows.delete_many({})
        self._restart_ids(1)
        if self.gridfs_fallback is not None:
            self.db.drop_collection("{}.chunks".format(GRIDFS_FALLBACK_COLLECTION))
            self.db.drop_collection("{}.files".format(GRIDFS_FALLBACK_COLLECTION))
        self.tuneup()
        self.m_logger.info('LaunchPad was RESET.')

    def _insert_wfs(self, wfs):
        if type(wfs) == Workflow:
            self.workflows.insert_one(wf.to_db_dict())
        else:
            self.workflows.insert_many(wf.to_db_dict() for wf in wfs)

    def _insert_fws(self, fws):
        if type(fws) == FireWork:
            self.fireworks.insert_one(fw.to_db_dict())
        else:
            self.fireworks.insert_many(fw.to_db_dict() for fw in fws)

    def get_fw_dict_by_id(self, fw_id):
        """
        Given firework id, return firework dict.

        Args:
            fw_id (int): firework id

        Returns:
            dict
        """
        fw_id = self._external_fwid_to_internal_fwid(fw_id)
        fw_dict = self.fireworks.find_one({'fw_id': fw_id}, sort=("launch_idx", DESCENDING))
        if not fw_dict:
            raise ValueError('No Firework exists with id: {}'.format(fw_id))
        return fw_dict

        """
        fw_dict = self.fireworks.find_one({'fw_id': fw_id})
        if not fw_dict:
            raise ValueError('No Firework exists with id: {}'.format(fw_id))
        # recreate launches from the launch collection
        launches = list(self.launches.find({'launch_id': {"$in": fw_dict['launches']}}))
        for l in launches:
            l["action"] = get_action_from_gridfs(l.get("action"), self.gridfs_fallback)
        fw_dict['launches'] = launches
        launches = list(self.launches.find({'launch_id': {"$in": fw_dict['archived_launches']}}))
        for l in launches:
            l["action"] = get_action_from_gridfs(l.get("action"), self.gridfs_fallback)
        fw_dict['archived_launches'] = launches
        return fw_dict
        """

    def get_fw_by_id(self, fw_id):
        """
        Given a Firework id, give back a Firework object.

        Args:
            fw_id (int): Firework id.

        Returns:
            Firework object
        """
        return Firework.from_dict(self.get_fw_dict_by_id(fw_id))

    def get_wf_by_fw_id(self, fw_id):
        """
        Given a Firework id, give back the Workflow containing that Firework.

        Args:
            fw_id (int)

        Returns:
            A Workflow object
        """
        links_dict = self.workflows.find_one({'nodes': fw_id})
        if not links_dict:
            raise ValueError("Could not find a Workflow with fw_id: {}".format(fw_id))
        fws = map(self.get_fw_by_id, links_dict["nodes"])
        return Workflow(fws, links_dict['links'], links_dict['name'],
                        links_dict['metadata'], links_dict['created_on'], links_dict['updated_on'])

    def get_wf_by_fw_id_lzyfw(self, fw_id):
        """
        Given a FireWork id, give back the Workflow containing that FireWork.

        Args:
            fw_id (int)

        Returns:
            A Workflow object
        """
        links_dict = self.workflows.find_one({'nodes': fw_id})
        if not links_dict:
            raise ValueError("Could not find a Workflow with fw_id: {}".format(fw_id))

        fws = []
        for fw_id in links_dict['nodes']:
            fws.append(LazyFirework(fw_id, self.fireworks, self.gridfs_fallback))
        # Check for fw_states in links_dict to conform with pre-optimized workflows
        if 'fw_states' in links_dict:
            fw_states = dict([(int(k), v) for (k, v) in links_dict['fw_states'].items()])
        else:
            fw_states = None

        return Workflow(fws, links_dict['links'], links_dict['name'],
                        links_dict['metadata'], links_dict['created_on'],
                        links_dict['updated_on'], fw_states)

    def delete_wf(self, fw_id, delete_launch_dirs=False):
        """
        Delete the workflow containing firework with the given id.

        Args:
            fw_id (int): Firework id
            delete_launch_dirs (bool): if True all the launch directories associated with
                the WF will be deleted as well, if possible.
        """
        links_dict = self.workflows.find_one({'nodes': fw_id})
        fw_ids = links_dict["nodes"]
        potential_launch_ids = []
        launch_ids = []
        for i in fw_ids:
            fw_dict = self.fireworks.find_one({'fw_id': i})
            potential_launch_ids += fw_dict["launches"] + fw_dict['archived_launches']


        # TODO THIS FUNCTION NEEDS TO BE CHANGED SIGNIFICANTLY TO REFLECT THE REMOVAL OF LAUNCHES
        for i in potential_launch_ids:  # only remove launches if no other fws refer to them
            if not self.fireworks.find_one({'$or': [{"launches": i}, {'archived_launches': i}],
                                            'fw_id': {"$nin": fw_ids}}, {'launch_id': 1}):
                launch_ids.append(i)

        if delete_launch_dirs:
            launch_dirs = []
            for i in launch_ids:
                # REALLY IMPORTANT TODO
                # this could be an issue if Firework gets initialized with cwd and then deletes
                # could end up deleting ~ or something drastic like that
                # add instruction to sort launch_idx by descending
                launch_dirs.append(self.fireworks.find_one({'fw_id': i},
                    {'launch_dir': 1}, sort=("launch_idx", DESCENDING))['launch_dir'])
            print("Remove folders %s" % launch_dirs)
            for d in launch_dirs:
                shutil.rmtree(d, ignore_errors=True)

        print("Remove fws %s" % fw_ids)
        print("Remove launches %s" % launch_ids)
        print("Removing workflow.")
        if self.gridfs_fallback is not None:
            for lid in launch_ids:
                for f in self.gridfs_fallback.find({"metadata.launch_id": lid}):
                    self.gridfs_fallback.delete(f._id)
        self.fireworks.delete_many({"fw_id": {"$in": fw_ids}})
        self.workflows.delete_one({'nodes': fw_id})

    def _get_wf_data(self, fw_id):
        wf_fields = ["state", "created_on", "name", "nodes"]
        fw_fields = ["state", "fw_id"]
        launch_fields = []

        if mode != "less":
            wf_fields.append("updated_on")
            fw_fields.extend(["name", "launches"])
            launch_fields.append("launch_dir")

        if mode == "reservations":
            launch_fields.append("state_history.reservation_id")

        if mode == "all":
            wf_fields = None
            launch_fields = None

        wf = self.workflows.find_one({"nodes": fw_id}, projection=wf_fields)
        fw_data = []
        id_name_map = {}
        launch_ids = []
        # need to fix this to include only the fireworks with the highest launch indexes
        for fw in self.fireworks.find({"fw_id": {"$in": wf["nodes"]}}, projection=fw_fields):
            if launch_fields:
                launch_ids.extend(fw["launches"])
            fw_data.append(fw)
            if mode != "less":
                id_name_map[fw["fw_id"]] = "%s--%d" % (fw["name"], fw["fw_id"])

        if launch_fields:
            launch_info = defaultdict(list)
            for l in self.fireworks.find({'fw_id': {"$in": fw_ids}}, projection=launch_fields):
                # logic here is unclear to me, not sure if this change works
                launch_info[i].append(l)
            for k, v in launch_info.items():
                fw_data[k]["launches"] = v

        wf["fw"] = fw_data
        return wf

    def get_fw_ids(self, query=None, sort=None, limit=0, count_only=False):
        """
        Return all the fw ids that match a query.

        Args:
            query (dict): representing a Mongo query
            sort [(str,str)]: sort argument in Pymongo format
            limit (int): limit the results
            count_only (bool): only return the count rather than explicit ids
            launches_mode (bool): query the launches collection instead of fireworks

        Returns:
            list: list of firework ids matching the query
        """
        fw_ids = []
        coll = "fireworks"
        criteria = query if query else {}

        if count_only:
            if limit:
                return ValueError("Cannot count_only and limit at the same time!")
            return getattr(self, coll).find(criteria, {}, sort=sort).count()

        for fw in getattr(self, coll).find(criteria, {"fw_id": True}, sort=sort).limit(limit):
            fw_ids.append(fw["fw_id"])
        return fw_ids

    def get_wf_ids(self, query=None, sort=None, limit=0, count_only=False):
        """
        Return one fw id for all workflows that match a query.

        Args:
            query (dict): representing a Mongo query
            sort [(str,str)]: sort argument in Pymongo format
            limit (int): limit the results
            count_only (bool): only return the count rather than explicit ids

        Returns:
            list: list of firework ids
        """
        wf_ids = []
        criteria = query if query else {}
        if count_only:
            return self.workflows.find(criteria, {"nodes": True}, sort=sort).limit(limit).count()

        for fw in self.workflows.find(criteria, {"nodes": True}, sort=sort).limit(limit):
            wf_ids.append(fw["nodes"][0])

        return wf_ids

    def tuneup(self, bkground=True):
        """
        Database tuneup: build indexes
        """
        self.m_logger.info('Performing db tune-up')

        self.m_logger.debug('Updating indices...')
        self.fireworks.create_index('fw_id', unique=True, background=bkground)
        self.fireworks.create_index({'launch_idx': -1}, unique=True, background=bkground)
        for f in ("state", 'spec._category', 'created_on', 'updated_on' 'name', 'launches'):
            self.fireworks.create_index(f, background=bkground)

        if GRIDFS_FALLBACK_COLLECTION is not None:
            files_collection = self.db["{}.files".format(GRIDFS_FALLBACK_COLLECTION)]
            files_collection.create_index('metadata.launch_id', unique=True, background=bkground)

        for f in ('name', 'created_on', 'updated_on', 'nodes'):
            self.workflows.create_index(f, background=bkground)

        for idx in self.user_indices:
            self.fireworks.create_index(idx, background=bkground)

        for idx in self.wf_user_indices:
            self.workflows.create_index(idx, background=bkground)

        # for frontend, which needs to sort on _id after querying on state
        self.fireworks.create_index([("state", DESCENDING), ("_id", DESCENDING)], background=bkground)
        self.fireworks.create_index([("state", DESCENDING), ("spec._priority", DESCENDING),
                                     ("created_on", DESCENDING)], background=bkground)
        self.fireworks.create_index([("state", DESCENDING), ("spec._priority", DESCENDING),
                                     ("created_on", ASCENDING)], background=bkground)
        self.workflows.create_index([("state", DESCENDING), ("_id", DESCENDING)], background=bkground)

        if not bkground:
            self.m_logger.debug('Compacting database...')
            try:
                self.db.command({'compact': 'fireworks'})
                self.db.command({'compact': 'launches'})
            except:
                self.m_logger.debug('Database compaction failed (not critical)')

    def _restart_ids(self, next_fw_id):
        """
        internal method used to reset firework id counters.

        Args:
            next_fw_id (int): id to give next Firework
            next_launch_id (int): id to give next Launch
        """
        # TODO removed next_launch_id
        self.fw_id_assigner.delete_many({})
        self.fw_id_assigner.find_one_and_replace({'_id': -1},
                                                 {'next_fw_id': next_fw_id}, upsert=True)
        self.m_logger.debug(
            'RESTARTED fw_id, launch_id to ({}, {})'.format(next_fw_id, next_launch_id))

    def _check_fw_for_uniqueness(self, m_fw):
        """
        Check if there are duplicates. If not unique, a new id is assigned and the workflow
        refreshed.

        Args:
            m_fw (Firework)

        Returns:
            bool: True if the firework is unique
        """
        if not self._steal_launches(m_fw):
            self.m_logger.debug('FW with id: {} is unique!'.format(m_fw.fw_id))
            return True
        self._upsert_fws([m_fw])  # update the DB with the new launches
        self._refresh_wf(m_fw.fw_id)  # since we updated a state, we need to refresh the WF again
        return False

    def _get_a_fw_to_run(self, query=None, fw_id=None, launch_idx=-1, checkout=True):
        """
        Get the next ready firework to run.

        Args:
            query (dict)
            fw_id (int): If given the query is updated.
                Note: We want to return None if this specific FW  doesn't exist anymore. This is
                because our queue params might have been tailored to this FW.
            checkout (bool): if True, check out the matching firework and set state=RESERVED

        Returns:
            Firework
        """
        # TODO either remove launch_idx as a parameter or actually use it
        # rather than just getting the most reent launch index.
        # Make use of launc_idx to make the searches safer. By sorting in descending
        # order of launch_idx, you get the most recent fw
        m_query = dict(query) if query else {}  # make a defensive copy
        m_query['state'] = 'READY'
        sortby = [("spec._priority", DESCENDING), ("launch_idx", DESCENDING)]

        if SORT_FWS.upper() == "FIFO":
            sortby.append(("created_on", ASCENDING))
        elif SORT_FWS.upper() == "FILO":
            sortby.append(("created_on", DESCENDING))

        # Override query if fw_id defined
        if fw_id:
            m_query = {"fw_id": fw_id, "state": {'$in': ['READY', 'RESERVED']}}

        while True:
            # check out the matching firework, depending on the query set by the FWorker
            if checkout:
                m_fw = self.fireworks.find_one_and_update(m_query,
                                                          {'$set': {'state': 'RESERVED',
                                                           'updated_on': datetime.datetime.utcnow()}},
                                                          sort=sortby)
            else:
                m_fw = self.fireworks.find_one(m_query, {'fw_id': 1, 'spec': 1}, sort=sortby)

            if not m_fw:
                return None
            m_fw = self.get_fw_by_id(m_fw['fw_id'], m_fw['launch_idx'])
            if self._check_fw_for_uniqueness(m_fw):
                return m_fw

    def get_fw_ids_from_reservation_id(self, reservation_id):
        """
        Given the reservation id, return the list of firework ids.

        Args:
            reservation_id (int)

        Returns:
            [int]: list of firework ids.
        """
        fw_ids = []
        fws = self.fireworks.find({"state_history.reservation_id": reservation_id},
                                      {'fw_id': 1})
        # TODO AVOIDED DUPLICATES
        return list(set([fw['fw_id'] for fw in fws]))

    def cancel_reservation_by_reservation_id(self, reservation_id):
        """
        Given the reservation id, cancel the reservation and rerun the corresponding fireworks.
        """
        fw = self.fireworks.find_one({"state_history.reservation_id": reservation_id},
                                      {'fw_id': 1})
        if fw:
            self.cancel_reservation(fw['fw_id'])
        else:
            self.m_logger.info("Can't find any reserved jobs with reservation id: {}".format(reservation_id))

    def get_reservation_id_from_fw_id(self, fw_id):
        """
        Given the firework id, return the reservation id
        """
        # Should this require launch_idx?
        fw = self.fireworks.find_one({'fw_id': fw_id}, sort={'launch_idx': DESCENDING})
        if fw:
            for d in fw['state_history']:
                if 'reservation_id' in d:
                    return d['reservation_id']

    def cancel_reservation(self, fw_id):
        """#
        given the launch id, cancel the reservation and rerun the fireworks
        """
        # Should this require launch_idx? I think not because only most recent
        # launch should be reserved. But maybe it's safer since this is called
        # by cancel_reservation_by_reservation_id
        m_fw = self.get_fw_by_id(fw_id)
        m_fw.state = 'READY'
        self.fireworks.find_one_and_replace({'fw_id': m_fw.fw_id, "state": "RESERVED"},
                                           m_fw.to_db_dict(), upsert=True)

        self.rerun_fw(m_fw.fw_id, rerun_duplicates=False)

    def detect_unreserved(self, expiration_secs=RESERVATION_EXPIRATION_SECS, rerun=False):
        """
        Return the reserved launch ids that have not been updated for a while.

        Args:
            expiration_secs (seconds): time limit
            rerun (bool): if True, the expired reservations are cancelled and the fireworks rerun.

        Returns:
            [int]: list of expired lacunh ids
        """
        # TODO might be good to move some of this into the abstract launchpad
        # and have a get_bad_launch_data() function of sorts, can also be used
        # for detect_lost_runs
        bad_launch_ids = []
        now_time = datetime.datetime.utcnow()
        cutoff_timestr = (now_time - datetime.timedelta(seconds=expiration_secs)).isoformat()
        bad_launch_data = self.fireworks.find({'state': 'RESERVED',
                                              'state_history':
                                                  {'$elemMatch':
                                                       {'state': 'RESERVED',
                                                        'updated_on': {'$lte': cutoff_timestr}
                                                        }
                                                   }
                                              },
                                             {'fw_id': 1})
        # not sure if can just remove what was here
        if rerun:
            for fw_id in bad_launch_data:
                self.cancel_reservation(fw_id)
        return bad_launch_data

    def set_reservation_id(self, fw_id, reservation_id, launch_idx=-1):
        """
        Set reservation id to the launch corresponding to the given launch id.

        Args:
            launch_id (int)
            reservation_id (int)
        """
        m_fw = self.get_fw_by_id(fw_id)
        m_fw.set_reservation_id(reservation_id)
        self.fireworks.find_one_and_replace({'fw_id': fw_id, 'launch_idx': m_fw.launch_idx},
                                            m_fw.to_db_dict())

    def change_launch_dir(self, fw_id, launch_dir, launch_idx=-1):
        """#
        Change the launch directory corresponding to the given launch id.

        Args:
            launch_id (int)
            launch_dir (str): path to the new launch directory.
        """
        m_fw = self.get_fw_by_id(fw_id, launch_idx)
        m_fw.launch_dir = launch_dir
        self.fireworks.find_one_and_replace({'fw_id': m_fw.fw_id, 'launch_idx': m_fw.launch_idx},
                                            m_fw.to_db_dict(), upsert=True)

    def restore_backup_data(self, fw_id, launch_idx):
        """
        For the given launch id and firework id, restore the back up data.
        """
        if fw_id in self.backup_fw_data:
            self.fireworks.find_one_and_replace({'fw_id': fw_id, 'launch_idx': launch_idx},
                                                self.backup_fw_data[fw_id])

    def _checkin_fw(self, m_fw, action, state):
        # might be able to remove DocumentTooLarge check if launches are all separate?
        try:
            self.fireworks.find_one_and_replace({'fw_id': m_fw.fw_id, 'launch_idx': m_fw.launch_idx},
                                               m_fw.to_db_dict(), upsert=True)
        except DocumentTooLarge as err:
            fw_db_dict = m_fw.to_db_dict()
            action_dict = fw_db_dict.get("action", None)
            if not action_dict:
                # in case the action is empty and it is not the source of
                # the error, raise the exception again.
                raise
            if self.gridfs_fallback is None:
                err.args = (err.args[0]
                            + '. Set GRIDFS_FALLBACK_COLLECTION in FW_config.yaml'
                              ' to a value different from None',)
                raise err

            # encoding required for python2/3 compatibility.
            action_id = self.gridfs_fallback.put(json.dumps(action_dict), encoding="utf-8",
                                                 metadata={"fw_id": fw_id})
            fw_db_dict["action"] = {"gridfs_id": str(action_id)}
            self.m_logger.warning("The size of the launch document was too large. Saving "
                               "the action in gridfs.")

            self.fireworks.find_one_and_replace({'fw_id': m_fw.fw_id, 'launch_idx': m_fw.launch_idx},
                                               fw_db_dict, upsert=True)


        # find all the fws that have this launch
        for fw in self._get_duplicates(m_fw.wf_id):
            self._refresh_wf(fw.fw_id)

    def _find_fws(self, fw_id, launch_idx=-1, allowed_states=None, find_one=False):
        query_dict = {'fw_id': fw_id, 'launch_idx': launch_idx}
        if not (allowed_states is None):
            query_dict['state'] = {'$in': [allowed_states]}
        if find_one:
            return self.fireworks.find_one(query_dict)
        else:
            return self.fireworks.find(query_dict)

    def _update_fw(self, m_fw):
        # maybe need to include launch_idx?
        self.fireworks.update_one({'fw_id': m_fw.fw_id, 'state': 'RUNNING'},
                                 {'$set': {'state_history': m_fw.to_db_dict()['state_history'],
                                           'trackers': [t.to_dict() for t in m_fw.trackers]}})

    def get_new_fw_id(self, quantity=1):
        """
        Checkout the next Firework id

        Args:
            quantity (int): optionally ask for many ids, otherwise defaults to 1
                            this then returns the *first* fw_id in that range
        """
        try:
            return self.fw_id_assigner.find_one_and_update({}, {'$inc': {'next_fw_id': quantity}})['next_fw_id']
        except:
            raise ValueError("Could not get next FW id! If you have not yet initialized the database,"
                             " please do so by performing a database reset (e.g., lpad reset)")

    def _upsert_fws(self, fws, reassign_all=False):
        """# need to address enumerate for upsert
        Insert the fireworks to the 'fireworks' collection.

        Args:
            fws ([Firework]): list of fireworks
            reassign_all (bool): if True, reassign the firework ids. The ids are also reassigned
                if the current firework ids are negative.

        Returns:
            dict: mapping between old and new Firework ids
        """
        old_new = {}
        # sort the FWs by id, then the new FW_ids will match the order of the old ones...
        fws.sort(key=lambda x: x.fw_id)

        if reassign_all:
            used_ids = []
            # we can request multiple fw_ids up front
            # this is the FIRST fw_id we should use
            # TODO need to change this so multiple fireworks can have the same fw_id
            first_new_id = self.get_new_fw_id(quantity=len(fws))

            for new_id, fw  in enumerate(fws, start=first_new_id):
                old_new[fw.fw_id] = new_id
                fw.fw_id = new_id
                used_ids.append(new_id)
            # delete/add in bulk
            self.fireworks.delete_many({'fw_id': {'$in': used_ids}})
            self.fireworks.insert_many((fw.to_db_dict() for fw in fws))
        else:
            for fw in fws:
                if fw.fw_id < 0:
                    new_id = self.get_new_fw_id()
                    old_new[fw.fw_id] = new_id
                    fw.fw_id = new_id

                self.fireworks.find_one_and_replace({'fw_id': fw.fw_id,\
                                                    'launch_idx': fw.launch_idx},
                                                    fw.to_db_dict(),
                                                    upsert=True)

        return old_new

    def _get_duplicates(self, fw_id):
        """#
        Returns the duplicates of fw_id, as already determined
        by steal_launches.
        """
        # TODO spec should contain a list of duplicates if fw has duplicates in the db
        f = self.fireworks.find_one({"fw_id": fw_id, "duplicates": {"$exists": True}})
        if f:
            for d in self.fireworks.find({"fw_id": {"$in": f['duplicates']},
                                          "fw_id": {"$ne": fw_id}}, {"fw_id": 1}):
                duplicates.append(d['fw_id'])
        return list(set(duplicates))

    def _recover(self, fw_id, recover_launch = None):
        """#
        Function to get recovery data for a given fw
        Args:
            fw_id (int): fw id to get recovery data for
            launch_id (int or 'last'): launch_id to get recovery data for, if 'last'
                recovery data is generated from last launch
        """
        if recover_launch is not None:
            m_fw = self.get_fw_by_id(fw_id)
            recovery = m_fw.state_history[-1].get("checkpoint")
            recovery.update({'_prev_dir': launch.launch_dir,
                             '_launch_id': launch.launch_id})
            # Launch recovery
            recovery.update({'_mode': recover_mode})
            set_spec = {'$set': {'spec._recovery': recovery}}
            if recover_mode == 'prev_dir':
                prev_dir = m_fw.launch_dir
                set_spec['$set']['spec._launch_dir'] = prev_dir
            self.fireworks.find_one_and_update({"fw_id": fw_id, "launch_idx": -1}, set_spec)

        # If no launch recovery specified, unset the firework recovery spec
        else:
            set_spec = {"$unset":{"spec._recovery":""}}
            self.fireworks.find_one_and_update({"fw_id":fw_id, "launch_idx": -1}, set_spec)

    def _refresh_wf(self, fw_id, state=None, allowed_states=None):
        """#
        Update the FW state of all jobs in workflow.

        Args:
            fw_id (int): the parent fw_id - children will be refreshed
        """
        # TODO: time how long it took to refresh the WF!
        # TODO: need a try-except here, high probability of failure if incorrect action supplied
        query_dict = {'fw_id': fw_id}
        if not (allowed_states is None):
            if type(allowed_states) == str:
                query_dict['state'] = allowed_states
            else:
                query_dict['state'] = {'$in': allowed_states}
        command_dict = {'$set':
                           {
                            'updated_on': datetime.datetime.utcnow()
                           }
                       }
        if not (state is None):
            command_dict['$set']['state'] = state
        f = self.fireworks.find_one_and_update(query_dict, command_dict)

        if f:
            try:
                with WFLock(self, fw_id):
                    wf = self.get_wf_by_fw_id_lzyfw(fw_id)
                    updated_ids = wf.refresh(fw_id)
                    self._update_wf(wf, updated_ids)
            except LockedWorkflowError:
                self.m_logger.info("fw_id {} locked. Can't refresh!".format(fw_id))
            except:
                # some kind of internal error - an example is that fws serialization changed due to
                # code updates and thus the Firework object can no longer be loaded from db description
                # Action: *manually* mark the fw and workflow as FIZZLED
                self.fireworks.find_one_and_update({"fw_id": fw_id}, {"$set": {"state": "FIZZLED"}})
                self.workflows.find_one_and_update({"nodes": fw_id}, {"$set": {"state": "FIZZLED"}})
                self.workflows.find_one_and_update({"nodes": fw_id},
                                                   {"$set": {"fw_states.{}".format(fw_id): "FIZZLED"}})
                import traceback
                err_message = "Error refreshing workflow. The full stack trace is: {}".format(
                    traceback.format_exc())
                raise RuntimeError(err_message)
        return f

    def _update_wf(self, wf, updated_ids):
        """
        Update the workflow with the updated firework ids.
        Note: must be called within an enclosing WFLock

        Args:
            wf (Workflow)
            updated_ids ([int]): list of firework ids
        """
        updated_fws = [wf.id_fw[fid] for fid in updated_ids]
        old_new = self._upsert_fws(updated_fws)
        wf._reassign_ids(old_new)

        # find a node for which the id did not change, so we can query on it to get WF
        query_node = None
        for f in wf.id_fw:
            if f not in old_new.values() or old_new.get(f, None) == f:
                query_node = f
                break

        assert query_node is not None
        if not self.workflows.find_one({'nodes': query_node}):
            raise ValueError("BAD QUERY_NODE! {}".format(query_node))
        # redo the links and fw_states
        wf = wf.to_db_dict()
        wf['locked'] = True  # preserve the lock!
        # TODO why does this lock never get undone?
        self.workflows.find_one_and_replace({'nodes': query_node}, wf)

    def _steal_launches(self, thief_fw):
        """
        Check if there are duplicates. If there are duplicates, the matching firework's launches
        are added to the launches of the given firework.

        Returns:
             bool: False if the given firework is unique
        """
        stolen = False
        if thief_fw.state in ['READY', 'RESERVED'] and '_dupefinder' in thief_fw.spec:
            m_dupefinder = thief_fw.spec['_dupefinder']
            # get the query that will limit the number of results to check as duplicates
            m_query = m_dupefinder.query(thief_fw.to_dict()["spec"])
            self.m_logger.debug('Querying for duplicates, fw_id: {}'.format(thief_fw.fw_id))
            # iterate through all potential duplicates in the DB
            for potential_match in self.fireworks.find(m_query):
                self.m_logger.debug('Verifying for duplicates, fw_ids: {}, {}'.format(
                    thief_fw.fw_id, potential_match['fw_id']))

                # TODO THIS SECTION SHOULD BE CLEANED UP
                # see if verification is needed, as this slows the process
                verified = False
                try:
                    m_dupefinder.verify({}, {})  # is implemented test

                except NotImplementedError:
                    verified = True  # no dupefinder.verify() implemented, skip verification

                except:  # we want to catch any exceptions from testing an empty dict, which the dupefinder might not be designed for
                    pass

                if not verified:
                    # dupefinder.verify() is implemented, let's call verify()
                    spec1 = dict(thief_fw.to_dict()['spec'])  # defensive copy
                    spec2 = dict(potential_match['spec'])  # defensive copy
                    verified = m_dupefinder.verify(spec1, spec2)

                if verified:
                    # steal the launches
                    # TODO changed this to steal fws not launches
                    thief_fw.add_duplicate(potential_match['fw_id'])
                    victim_fw = self.get_fw_by_id(potential_match['fw_id'])
                    victim_fw.add_duplicate(thief_fw.fw_id)
                    self._update_fw(victim_fw)
                    stolen = True
                    self.m_logger.info('Duplicate found! fwids {} and {}'.format(
                        thief_fw.fw_id, potential_match['fw_id']))
        return stolen

    def set_priority(self, fw_id, priority, launch_idx=-1):
        """#
        Set priority to the firework with the given id.

        Args:
            fw_id (int): firework id
            priority
        """
        self.fireworks.find_one_and_update({"fw_id": fw_id}, {'$set': {'spec._priority': priority}})

    def get_tracker_data(self, fw_id):
        """
        Args:
            fw_id (id): firework id

        Returns:
            [dict]: list tracker dicts
        """
        data = []
        for fw in self.fireworks.find({'fw_id': fw_id}):
            if 'trackers' in fw:
                trackers = [Tracker.from_dict(t) for t in fw['trackers']]
                data.append({'fw_id': fw['fw_id'], 'trackers': trackers})
        return data

    def get_launchdir(self, fw_id, launch_idx=-1):
        """
        Returns the directory of the *most recent* launch of a fw_id
        Args:
            fw_id: (int) fw_id to get launch id for
            launch_idx: (int) index of the launch to get. Default is -1, which is most recent.
        """
        fw = self.get_fw_by_id(fw_id, lauch_idx)
        return fw.launch_dir


class LazyFirework(object):
    """
    A LazyFirework only has the fw_id, and retrieves other data just-in-time.
    This representation can speed up Workflow loading as only "important" FWs need to be
    fully loaded.
    """

    # Get these fields from DB when creating new FireWork object
    db_fields = ('name', 'fw_id', 'spec', 'created_on', 'state')
    db_launch_fields = ('launches', 'archived_launches')

    def __init__(self, fw_id, fw_coll, fallback_fs):
        """
        Args:
            fw_id (int): firework id
            fw_coll (pymongo.collection): fireworks collection
            launch_coll (pymongo.collection): launches collection
        """
        # This is the only attribute known w/o a DB query
        self.fw_id = fw_id
        self._fwc, self._ffs = fw_coll, fallback_fs
        self._launches = {k: False for k in self.db_launch_fields}
        self._fw, self._lids, self._state = None, None, None

    # FireWork methods

    # Treat state as special case as it is always required when accessing a Firework lazily
    # If the partial fw is not available the state is fetched independently
    @property
    def state(self):
        if self._fw is not None:
            self._state = self._fw.state
        elif self._state is None:
            self._state = self._fwc.find_one({'fw_id': self.fw_id}, projection=['state'])['state']
        return self._state

    @state.setter
    def state(self, state):
        self.partial_fw._state = state
        self.partial_fw.updated_on = datetime.datetime.utcnow()

    def to_dict(self):
        return self.full_fw.to_dict()

    def _rerun(self):
        self.full_fw._rerun()

    def to_db_dict(self):
        return self.full_fw.to_db_dict()

    def __str__(self):
        return 'LazyFireWork object: (id: {})'.format(self.fw_id)

    # Properties that shadow FireWork attributes

    @property
    def tasks(self):
        return self.partial_fw.tasks

    @tasks.setter
    def tasks(self, value):
        self.partial_fw.tasks = value

    @property
    def spec(self):
        return self.partial_fw.spec

    @spec.setter
    def spec(self, value):
        self.partial_fw.spec = value

    @property
    def name(self):
        return self.partial_fw.name

    @name.setter
    def name(self, value):
        self.partial_fw.name = value

    @property
    def created_on(self):
        return self.partial_fw.created_on

    @created_on.setter
    def created_on(self, value):
        self.partial_fw.created_on = value

    @property
    def updated_on(self):
        return self.partial_fw.updated_on

    @updated_on.setter
    def updated_on(self, value):
        self.partial_fw.updated_on = value

    @property
    def parents(self):
        if self._fw is not None:
            return self.partial_fw.parents
        else:
            return []

    @parents.setter
    def parents(self, value):
        self.partial_fw.parents = value

    # Properties that shadow FireWork attributes, but which are
    # fetched individually from the DB (i.e. launch objects)

    @launches.setter
    def launches(self, value):
        self._launches['launches'] = True
        self.partial_fw.launches = value

    @archived_launches.setter
    def archived_launches(self, value):
        self._launches['archived_launches'] = True
        self.partial_fw.archived_launches = value

    # Lazy properties that idempotently instantiate a FireWork object
    @property
    def partial_fw(self):
        if not self._fw:
            fields = list(self.db_fields) + list(self.db_launch_fields)
            data = self._fwc.find_one({'fw_id': self.fw_id}, projection=fields)
            launch_data = {}  # move some data to separate launch dict
            for key in self.db_launch_fields:
                launch_data[key] = data[key]
                del data[key]
            self._lids = launch_data
            self._fw = Firework.from_dict(data)
        return self._fw

    @property
    def full_fw(self):
        #map(self._get_launch_data, self.db_launch_fields)
        for launch_field in self.db_launch_fields:
            self._get_launch_data(launch_field)
        return self._fw

    # Get a type of Launch object

    '''
    def _get_launch_data(self, name):
        """
        Pull launch data individually for each field.

        Args:
            name (str): Name of field, e.g. 'archived_launches'.

        Returns:
            Launch obj (also propagated to self._fw)
        """
        fw = self.partial_fw  # assure stage 1
        if not self._launches[name]:
            launch_ids = self._lids[name]
            result = []
            if launch_ids:
                data = self._lc.find({'launch_id': {"$in": launch_ids}})
                for ld in data:
                    ld["action"] = get_action_from_gridfs(ld.get("action"), self._ffs)
                    result.append(Launch.from_dict(ld))

            setattr(fw, name, result)  # put into real FireWork obj
            self._launches[name] = True
        return getattr(fw, name)
    '''


def get_action_from_gridfs(action_dict, fallback_fs):
    """
    Helper function to obtain the correct dictionary of the FWAction associated
    with a launch. If necessary retrieves the information from gridfs based
    on its identifier, otherwise simply returns the dictionary in input.
    Should be used when accessing a launch to ensure the presence of the
    correct action dictionary.
    
    Args:
        action_dict (dict): the dictionary contained in the "action" key of a launch
            document.
        fallback_fs (GridFS): the GridFS with the actions exceeding the 16MB limit.
    Returns:
        dict: the dictionary of the action.
    """

    if not action_dict or "gridfs_id" not in action_dict:
        return action_dict

    action_gridfs_id = ObjectId(action_dict["gridfs_id"])

    action_data = fallback_fs.get(ObjectId(action_gridfs_id))
    return json.loads(action_data.read())
