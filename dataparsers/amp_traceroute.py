# This file is part of NNTSC
#
# Copyright (C) 2013 The University of Waikato, Hamilton, New Zealand
# Authors: Shane Alcock
#          Brendon Jones
#          Nathan Overall
#
# All rights reserved.
#
# This code has been developed by the WAND Network Research Group at the
# University of Waikato. For more information, please see
# http://www.wand.net.nz/
#
# This source code is proprietary to the University of Waikato and may not be
# redistributed, published or disclosed without prior permission from the
# University of Waikato and the WAND Network Research Group.
#
# Please report any bugs, questions or comments to contact@wand.net.nz
#
# $Id$


from sqlalchemy import Table, Column, Integer, \
    String, ForeignKey, UniqueConstraint, Index
from sqlalchemy.sql import text
from sqlalchemy.types import Integer, String
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.dialects import postgresql
from libnntsc.partition import PartitionedTable
import libnntscclient.logger as logger

STREAM_TABLE_NAME = "streams_amp_traceroute"
DATA_TABLE_NAME = "data_amp_traceroute"

amp_trace_streams = {}
partitions = None

def stream_table(db):
    """ Specify the description of a traceroute stream, to create the table """

    if STREAM_TABLE_NAME in db.metadata.tables:
        return STREAM_TABLE_NAME

    st = Table(STREAM_TABLE_NAME, db.metadata,
        Column('stream_id', Integer, ForeignKey("streams.id"),
                primary_key=True),
        Column('source', String, nullable=False),
        Column('destination', String, nullable=False),
        Column('packet_size', String, nullable=False),
        Column('address', postgresql.INET, nullable=False),
        UniqueConstraint('source', 'destination', 'packet_size', 'address'),
        useexisting=True,
    )

    Index('index_amp_traceroute_source', st.c.source)
    Index('index_amp_traceroute_destination', st.c.destination)

    return STREAM_TABLE_NAME

def data_table(db):
    """ Specify the description of traceroute data, used to create the table """

    if DATA_TABLE_NAME in db.metadata.tables:
        return DATA_TABLE_NAME

    dt = Table(DATA_TABLE_NAME, db.metadata,
        Column('stream_id', Integer, ForeignKey("streams.id"),
                nullable = False),
        Column('timestamp', Integer, nullable=False),
        Column('packet_size', Integer, nullable=False),
        Column('length', Integer, nullable=False),
        Column('error_type', Integer, nullable=False),
        Column('error_code', Integer, nullable=False),
        Column('hop_rtt', postgresql.ARRAY(Integer), nullable=False),
        Column('path', postgresql.ARRAY(postgresql.INET), nullable=False),
        useexisting=True,
    )

    return DATA_TABLE_NAME


def register(db):
    st_name = stream_table(db)
    dt_name = data_table(db)

    db.register_collection("amp", "traceroute", st_name, dt_name)


def create_existing_stream(stream_data):
    """ Extract the stream key from the stream data provided by NNTSC
        when the AMP module is first instantiated.
    """

    key = (str(stream_data["source"]), str(stream_data["destination"]),
        str(stream_data["address"]), str(stream_data["packet_size"]))

    amp_trace_streams[key] = stream_data["stream_id"]


def insert_stream(db, exp, source, dest, size, address, timestamp):
    """ Insert a new traceroute stream into the streams table """

    name = "traceroute %s:%s:%s:%s" % (source, dest, address, size)

    props = {"name":name, "source":source, "destination":dest,
            "packet_size":size, "datastyle":"traceroute",
            "address": address}

    colid, streamid = db.register_new_stream("amp", "traceroute", name,
            timestamp)

    if colid == -1:
        return -1

    # insert stream into our stream table
    st = db.metadata.tables[STREAM_TABLE_NAME]

    try:
        result = db.conn.execute(st.insert(), stream_id=streamid,
                source=source, destination=dest, packet_size=size,
                address=address, datastyle="traceroute")
    except IntegrityError, e:
        db.rollback_transaction()
        logger.log(e)
        return -1

    if streamid >= 0 and exp != None:
        exp.send((1, (colid, "amp_traceroute", streamid, props)))

    return streamid


def insert_data(db, exp, stream, ts, result):
    """ Insert data for a single traceroute test into the database """
    global partitions

    if partitions == None:
        partitions = PartitionedTable(db, DATA_TABLE_NAME, 60 * 60 * 24 * 7,
                ["timestamp", "stream_id", "packet_size"])
    partitions.update(ts)

    try:
        # sqlalchemy is again totally useless and makes it impossible to cast
        # types on insert, so lets do it ourselves.
        db.conn.execute(text("INSERT INTO %s ("
                    "stream_id, timestamp, packet_size, length, error_type, "
                    "error_code, hop_rtt, path) VALUES ("
                    ":stream_id, :timestamp, :packet_size, :length, "
                    ":error_type, :error_code, CAST(:hop_rtt AS integer[]),"
                    "CAST(:path AS inet[]))" % DATA_TABLE_NAME),
                    stream_id=stream, timestamp=ts, **result)
    except IntegrityError, e:
        db.rollback_transaction()
        logger.log(e)
        return -1

    exp.send((0, ("amp_traceroute", stream, ts, result)))
    db.commit_transaction()

    return 0


def process_data(db, exp, timestamp, data, source):
    """ Process data (which may have multiple paths) and insert into the DB """
    # For each path returned in the test data
    for d in data:
        if d["random"]:
            sizestr = "random"
        else:
            sizestr = str(d["packet_size"])

        d["source"] = source
        key = (source, d["target"], d['address'], sizestr)

        if key in amp_trace_streams:
            stream_id = amp_trace_streams[key]
        else:
            stream_id = insert_stream(db, exp, source, d["target"], sizestr,
                    d['address'], timestamp)

            if stream_id == -1:
                logger.log("AMPModule: Cannot create stream for:")
                logger.log("AMPModule: %s %s:%s:%s:%s\n" % (
                        "traceroute", source, d["target"], d["address"],
                        sizestr))
                return -1
            else:
                amp_trace_streams[key] = stream_id

        # TODO maybe we want to change the way ampsave gives us this data
        # so we don't need to change it up again
        d["path"] = [x["address"] for x in d["hops"]]
        d["hop_rtt"] = [x["rtt"] for x in d["hops"]]

        insert_data(db, exp, stream_id, timestamp, d)
        db.update_timestamp(stream_id, timestamp)

# vim: set sw=4 tabstop=4 softtabstop=4 expandtab :
