#
# This file is part of NNTSC.
#
# Copyright (C) 2013-2017 The University of Waikato, Hamilton, New Zealand.
#
# Authors: Shane Alcock
#          Brendon Jones
#
# All rights reserved.
#
# This code has been developed by the WAND Network Research Group at the
# University of Waikato. For further information please see
# http://www.wand.net.nz/
#
# NNTSC is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.
#
# NNTSC is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with NNTSC; if not, write to the Free Software Foundation, Inc.
# 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
#
# Please report any bugs, questions or comments to contact@wand.net.nz
#

from libnntsc.parsers.amp_icmp import AmpIcmpParser
import libnntscclient.logger as logger


class AmpTcppingParser(AmpIcmpParser):
    def __init__(self, db, influxdb=None):
        super(AmpTcppingParser, self).__init__(db, influxdb)

        self.streamtable = "streams_amp_tcpping"
        self.datatable = "data_amp_tcpping"
        self.colname = "amp_tcpping"
        self.source = "amp"
        self.module = "tcpping"

        self.streamcolumns = [
            {"name":"source", "type":"varchar", "null":False},
            {"name":"destination", "type":"varchar", "null":False},
            {"name":"port", "type":"integer", "null":False},
            {"name":"family", "type":"varchar", "null":False},
            {"name":"packet_size", "type":"varchar", "null":False},
        ]

        self.uniquecolumns = ['source', 'destination', 'port', 'family',
                'packet_size']
        self.streamindexes = [
            {"name": "", "columns": ['source']},
            {"name": "", "columns": ['destination']},
            {"name": "", "columns": ['port']}
        ]

        self.datacolumns = [
            {"name":"median", "type":"integer", "null":True},
            {"name":"packet_size", "type":"smallint", "null":False},
            {"name":"loss", "type":"smallint", "null":True},
            {"name":"results", "type":"smallint", "null":True},
            {"name":"icmperrors", "type":"smallint", "null":True},
            {"name":"rtts", "type":"integer[]", "null":True},
            {"name":"lossrate", "type":"float", "null":True},
            #{"name":"replyflags", "type":"smallint", "null":True},
            #{"name":"icmptype", "type":"smallint", "null":True},
            #{"name":"icmpcode", "type":"smallint", "null":True},
        ]

        #self.dataindexes = []

    def create_existing_stream(self, stream_data):
        src = str(stream_data['source'])
        dest = str(stream_data['destination'])
        family = str(stream_data['family'])
        port = str(stream_data['port'])
        size = str(stream_data['packet_size'])

        key = (src, dest, port, family, size)
        self.streams[key] = stream_data['stream_id']

    def _stream_properties(self, source, result):
        props = {}

        if 'target' not in result:
            logger.log("Error: no target specified in %s result" % \
                    (self.colname))
            return None, None

        if 'port' not in result:
            logger.log("Error: no port specified in %s result" % \
                    (self.colname))
            return None, None

        if 'address' not in result:
            logger.log("Error: no address specified in %s result" % \
                    (self.colname))
            return None, None

        if '.' in result['address']:
            family = "ipv4"
        else:
            family = "ipv6"

        if result['random']:
            sizestr = "random"
        else:
            if 'packet_size' not in result:
                logger.log("Error: no packet size specified in %s result" % \
                        (self.colname))
                return None, None
            sizestr = str(result['packet_size'])

        props['source'] = source
        props['destination'] = result['target']
        props['port'] = str(result['port'])
        props['family'] = family
        props['packet_size'] = sizestr

        key = (props['source'], props['destination'], props['port'], \
                props['family'], props['packet_size'])
        return props, key


    def _update_stream(self, observed, streamid, data):
        if streamid not in observed:
            observed[streamid] = {
                "loss": None,
                "rtts":[],
                "icmperrors": None,
                "median":None,
                "packet_size": data["packet_size"],
                "results": None
            }

        stats = observed[streamid]

        if 'icmptype' in data and data['icmptype'] is not None:
            # count the number of errors (non-zero type) received
            stats["icmperrors"] = self._add_maybe_none(stats["icmperrors"],
                    int(bool(data['icmptype'])))

        if 'loss' in data and data['loss'] is not None:
            stats["loss"] = self._add_maybe_none(stats["loss"], data["loss"])

        if 'rtt' in data and data['rtt'] is not None:
            observed[streamid]["rtts"].append(data['rtt'])

        # rtt will be > 0 or loss > 0 if there was a measurement result
        if data.get('rtt', False) or data.get('loss', False):
            stats["results"] = self._add_maybe_none(stats["results"], 1)

    def _aggregate_streamdata(self, streamdata):
        streamdata["rtts"].sort()
        streamdata["median"] = self._find_median(streamdata["rtts"])

        # Add None entries to our array for lost measurements -- we
        # have to wait until now to add them otherwise they'll mess
        # with our median calculation
        if streamdata["loss"]:
            streamdata["rtts"] += [None] * streamdata["loss"]

        if streamdata["icmperrors"]:
            streamdata["rtts"] += [None] * streamdata["icmperrors"]

        if streamdata["results"]:
            streamdata["lossrate"] = streamdata["loss"] / float(streamdata["results"])
        else:
            streamdata["lossrate"] = None


# vim: set sw=4 tabstop=4 softtabstop=4 expandtab :
