"""
Copyright 2015 Quantopian Inc.

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.
"""

import logging
import sys
import time
from pymongo.errors import AutoReconnect, OperationFailure

# How long we are willing to attempt to reconnect when the replicaset
# fails over.  We double the delay between each attempt.
MAX_RECONNECT_TIME = 60
MAX_SLEEP = 5
RECONNECT_INITIAL_DELAY = 1


class MongoReconnectFailure(Exception):
    """
    Exception raised when we fail AutoReconnect more than
    the allowed number of times.
    """
    pass


class DurableCursor(object):
    """
    Wrapper class around a pymongo cursor that detects and handles
    replica set failovers and cursor timeouts.  Upon successful
    reconnect this class automatically skips over previously returned
    records, resuming iteration as though no error occurred.
    """

    # Replace this or override it in a subclass for different logging.
    logger = logging.getLogger(__name__)

    def __init__(
            self,
            collection,
            spec=None,
            fields=None,
            sort=None,
            slave_okay=True,
            hint=None,
            tailable=False,
            max_reconnect_time=MAX_RECONNECT_TIME,
            initial_reconnect_interval=RECONNECT_INITIAL_DELAY,
            skip=0,
            limit=0,
            disconnect_on_timeout=True,
            **kwargs):

        self.collection = collection
        self.spec = spec
        self.fields = fields
        self.sort = sort
        self.slave_okay = slave_okay
        self.hint = hint
        self.tailable = tailable

        # The number of times we attempt to reconnect to a replica set.
        self.max_reconnect_time = max_reconnect_time

        # The amount of time, in seconds, between reconnect attempts.
        self.initial_reconnect_interval = initial_reconnect_interval

        self.counter = self.skip = skip
        self.limit = limit
        self.disconnect_on_timeout = disconnect_on_timeout
        self.kwargs = kwargs

        self.cursor = self.fetch_cursor(self.counter, self.kwargs)

    def __iter__(self):
        return self

    def fetch_cursor(self, count, cursor_kwargs):
        """
        Gets a cursor for the options set in the object.

        Used to both get the initial cursor and reloaded cursor.

        The difference between initial load and reload is the
        value of count.
        count is 0 on initial load,
        where as count > 0 is used during reload.
        """
        limit_is_zero = False  # as opposed to 0 meaning no limit
        if self.limit:
            limit = self.limit - (count - self.skip)
            if limit <= 0:
                limit = 1
                limit_is_zero = True
        else:
            limit = 0

        cursor = self.collection.find(
            spec=self.spec,
            fields=self.fields,
            sort=self.sort,
            slave_okay=self.slave_okay,
            tailable=self.tailable,
            skip=count,
            limit=limit,
            hint=self.hint,
            **cursor_kwargs
        )
        if limit_is_zero:
            # we can't use 0, since that's no limit, so instead we set it to 1
            # and then move the cursor forward by one element here
            next(cursor, None)
        return cursor

    def reload_cursor(self):
        """
        Reload our internal pymongo cursor with a new query.  Use
        self.counter to skip the records we've already
        streamed. Assuming the database remains unchanged we should be
        able to call this method as many times as we want without
        affecting the events we stream.
        """
        self.cursor = self.fetch_cursor(self.counter, self.kwargs)

    @property
    def alive(self):
        return self.tailable and self.cursor.alive

    def next(self):
        try:
            next_record = self.cursor.next()

        # OperationFailure is raised when an operation fails inside
        # the remote DB.  This most commonly occurs when our cursor
        # has been inactive for 10 minutes or more.
        except OperationFailure as exc:
            self.logger.info("""
Attempting to handle cursor timeout.
OperationFailure exception catches a lot of failure cases.
The current exception is actually:
{exc}
TODO: Inspect the exc name and only reload the cursor on timeouts.

The query spec that timed out was:
{spec}
""".strip().format(exc=exc, spec=self.spec))

            # Try to reload the cursor and continue where we left off
            self.reload_cursor()
            next_record = self.cursor.next()
            self.logger.info("Cursor reload after timeout successful.")

        # AutoReconnect is raised when the primary node fails and we
        # attempt to reconnect to the replica set.
        except AutoReconnect:
            self.logger.info("Got AutoReconnect; attempting recovery",
                             exc_info=sys.exc_info())

            # Try for up to self.max_reconnect_time to reconnect to
            # the replicaset before giving up.  If the reconnect is
            # successful, we return success == True along with the
            # next record to return. Otherwise we return (False,
            # None).
            next_record = self.try_reconnect()

        # Increment count before returning so we know how many records
        # to skip if a failure occurs later.
        self.counter += 1
        return next_record

    def try_reconnect(self, get_next=True):
        """
        Attempt to reconnect to our collection after a replicaset failover.
        Returns a flag indicating whether the reconnect attempt was successful
        along with the next record to return if applicable. This should only
        be called when trying to recover from an AutoReconnect exception.
        """
        attempts = 0
        round = 1
        start = time.time()
        interval = self.initial_reconnect_interval
        disconnected = False
        max_time = self.max_reconnect_time

        while True:
            try:
                # Attempt to reload and get the next batch.
                self.reload_cursor()
                return self.cursor.next() if get_next else True

            # Replica set hasn't come online yet.
            except AutoReconnect:
                if time.time() - start > max_time:
                    if not self.disconnect_on_timeout or disconnected:
                        break
                    self.cursor.collection.database.connection.disconnect()
                    disconnected = True
                    interval = self.initial_reconnect_interval
                    round = 2
                    attempts = 0
                    max_time *= 2
                    self.logger.warning('Resetting clock for round 2 after '
                                        'disconnecting')
                delta = time.time() - start
                self.logger.warning(
                    "AutoReconnecting, try %d.%d, (%.1f seconds elapsed)" %
                    (round, attempts, delta))
                # Give the database time to reload between attempts.
                time.sleep(interval)
                interval = min(interval * 2, MAX_SLEEP)
                attempts += 1

        self.logger.error('Replica set reconnect failed.')
        raise MongoReconnectFailure()

    def count(self, with_limit_and_skip=False):
        while True:
            try:
                return self.cursor.count(
                    with_limit_and_skip=with_limit_and_skip)
            except AutoReconnect:
                self.try_reconnect(get_next=False)
