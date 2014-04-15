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


import libnntscclient.logger as logger
from libnntsc.dberrorcodes import *

STREAM_TABLE_NAME = "streams_rrd_muninbytes"
DATA_TABLE_NAME = "data_rrd_muninbytes"
COLNAME = "rrd_muninbytes"

def stream_table(db):

    streamcols = [ \
        {"name":"filename", "type":"varchar", "null":False},
        {"name":"switch", "type":"varchar", "null":False},
        {"name":"interface", "type":"varchar", "null":False},
        {"name":"interfacelabel", "type":"varchar"},
        {"name":"direction", "type":"varchar", "null":False},
        {"name":"minres", "type":"integer", "null":False, "default":"300"},
        {"name":"highrows", "type":"integer", "null":False, "default":"1008"}
    ]

    uniqcols = ['filename', 'interface', 'switch', 'direction']

    err = db.create_streams_table(STREAM_TABLE_NAME, streamcols, uniqcols)

    if err != DB_NO_ERROR:
        return None
    return STREAM_TABLE_NAME


def data_table(db):

    datacols = [ \
        {"name":"bytes", "type":"bigint"}
    ]

    err =  db.create_data_table(DATA_TABLE_NAME, datacols)
    if err != DB_NO_ERROR:
        return None
    return DATA_TABLE_NAME


def insert_stream(db, exp, name, filename, switch, interface, dir, minres,
        rows, label):

    props = {"filename":filename, "switch":switch,
            "interface":interface, "direction":dir, "minres":minres,
            "highrows":rows, "interfacelabel":label}

    while 1:
        colid, streamid = db.insert_stream(STREAM_TABLE_NAME, DATA_TABLE_NAME, 
                "rrd", "muninbytes", name, 0, props)
        
        errorcode = DB_NO_ERROR
        if colid < 0:
            errorcode = streamid

        if streamid < 0:
            errorcode = streamid

        if errorcode == DB_OPERATIONAL_ERROR or errorcode == DB_QUERY_TIMEOUT:
            continue
        if errorcode != DB_NO_ERROR:
            return errorcode

        err = db.commit_streams()
        if err == DB_QUERY_TIMEOUT or err == DB_OPERATIONAL_ERROR:
            continue
        if err != DB_NO_ERROR:
            return err
        break
 

    if exp == None:
        return streamid
    props["name"] = name
    exp.publishStream(colid, COLNAME, streamid, props)
    return streamid



def insert_data(db, exp, stream, ts, line):
    assert(len(line) == 1)

    exportdict = {}

    line_map = {0:"bytes"}

    for i in range(0, len(line)):
        if line[i] == None:
            val = None
        else:
            val = int(line[i])

        exportdict[line_map[i]] = val

    err = db.insert_data(DATA_TABLE_NAME, "rrd_muninbytes", stream, ts,
            exportdict)
    if err != DB_NO_ERROR:
        return err
    if exp != None:
        exp.publishLiveData(COLNAME, stream, ts, exportdict)
    return DB_NO_ERROR


# vim: set sw=4 tabstop=4 softtabstop=4 expandtab :
