import json
from operator import attrgetter
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet
from ryu.lib.packet import ethernet
from ryu.lib.packet import ether_types
from ryu.lib import hub


class SimpleSwitch13(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(SimpleSwitch13, self).__init__(*args, **kwargs)
        self.mac_to_port = {}
        self.src_to_meter = {}
        self.meter_to_src = {}
        self.n_meter = {}
        self.port_to_meter = {}
        with open('/home/mininet/Rene/subs.json') as data_file:    
            self.subs = json.load(data_file)
        self.max_rate = 40000
        self.default_rate = 5000
        self.rate_request = {}
        self.rate_allocated = {}
        self.rate_used = {}
        self.rate_used_mod = {}
        self.datapaths = {}
        self.monitor_thread = hub.spawn(self._monitor)
        self.sleep = 5
        self.port_speed = {}
        self.port_prev = {}
        self.meter_speed = {}
        self.meter_prev = {}
        
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        
        # install table-miss flow entry
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                         ofproto.OFPCML_NO_BUFFER)]  
        match = parser.OFPMatch()
        self._add_flow(datapath, 0, match, actions, 1)

        # add resubmit flow
        inst = [parser.OFPInstructionGotoTable(1)]
        mod = parser.OFPFlowMod(datapath=datapath, priority=0, match=match, 
                                instructions=inst, table_id=0)
        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPStateChange,
                [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change_handler(self, ev):
        datapath = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            if not datapath.id in self.datapaths:
                self.logger.debug('register datapath: %016x', datapath.id)
                self.datapaths[datapath.id] = datapath
                # Datapath's dictionaries for BW measurement
                self.port_speed[datapath.id]    = {}
                self.port_prev[datapath.id]     = {}
                self.meter_speed[datapath.id]   = {}
                self.meter_prev[datapath.id]    = {}
                # Switch dictionaries
                self.n_meter[datapath.id]       = 0
                self.mac_to_port[datapath.id]   = {}
                self.src_to_meter[datapath.id]  = {}
                self.port_to_meter[datapath.id] = {}
                self.meter_to_src[datapath.id]  = {}
                self.rate_request[datapath.id]  = {}
                self.rate_allocated[datapath.id] = {}
                self.rate_used[datapath.id]     = {}
                self.rate_used_mod[datapath.id] = {}
        elif ev.state == DEAD_DISPATCHER:
            if datapath.id in self.datapaths:
                self.logger.debug('unregister datapath: %016x', datapath.id)
                # Deleting datapath's dictionaries
                del self.datapaths[datapath.id]
                del self.port_speed[datapath.id]
                del self.port_prev[datapath.id]
                del self.meter_speed[datapath.id]
                del self.meter_prev[datapath.id]
                # Deleting switch dictionaries
                del self.mac_to_port[datapath.id]
                del self.n_meter[datapath.id]
                del self.src_to_meter[datapath.id]
                del self.port_to_meter[datapath.id]
                del self.meter_to_src[datapath.id]
                del self.rate_request[datapath.id]
                del self.rate_allocated[datapath.id]
                del self.rate_used[datapath.id]
                del self.rate_used_mod[datapath.id]

    def _monitor(self):
        while True:
            for dp in self.datapaths.values():
                self._request_stats(dp)
            hub.sleep(self.sleep)

    def _get_speed(self, now, pre, period):
        return 8*((now-pre)/(period*1000.0))

    def _request_stats(self, datapath):
        self.logger.debug('send stats request: %016x', datapath.id)
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        req = parser.OFPMeterStatsRequest(datapath, 0, ofproto.OFPM_ALL)
        datapath.send_msg(req)

    @set_ev_cls(ofp_event.EventOFPMeterStatsReply, MAIN_DISPATCHER)
    def _meter_stats_reply_handler(self, ev):
        body = ev.msg.body
        dpid = ev.msg.datapath.id
        self.logger.info('datapath         meter_id   Kbps  ')
        self.logger.info('---------------- -------- --------')

        for stat in sorted(body, key=attrgetter('meter_id')):
            self.meter_speed[dpid][stat.meter_id] = self._get_speed(stat.byte_in_count, self.meter_prev[dpid].get(stat.meter_id, 0), self.sleep)
            self.logger.info("%016x %08x %6.1f",dpid, stat.meter_id, self.meter_speed[dpid].get(stat.meter_id, 0))
            if stat.meter_id in self.meter_to_src[dpid]:
                src = self.meter_to_src[dpid][stat.meter_id]
                port = self.mac_to_port[dpid][src]
                self.rate_used[dpid].setdefault(port, {})
                self.rate_used_mod[dpid].setdefault(port, {})
                self.rate_used[dpid][port][src] = self.meter_speed[dpid][stat.meter_id]
                if (self.rate_used[dpid][port][src] >= int(self.rate_allocated[dpid][port][src]*0.7) 
                    and (self.rate_allocated[dpid][port][src] != self.rate_request[dpid][port][src])):
                    self.rate_used_mod[dpid][port][src] = int(self.rate_used[dpid][port][src]*1.5)
                    hub.spawn(self._mod_port_meters, dpid, port)
                else:
                    self.rate_used_mod[dpid][port][src] = self.rate_used[dpid][port][src]
            self.meter_prev[dpid][stat.meter_id] = stat.byte_in_count

    def _mod_port_meters(self, dpid, in_port):
        self.logger.debug('Datapath: %s modifying port %d meters', dpid, in_port)
        datapath = self.datapaths[dpid]
        ofproto  = datapath.ofproto
        parser   = datapath.ofproto_parser
        cmd      = ofproto.OFPMC_MODIFY
        prev_allocated = self.rate_allocated[dpid].get(in_port, {})
        self.rate_allocated[dpid][in_port] = self._rate_control(self.max_rate, self.rate_request[dpid][in_port], self.rate_used_mod[dpid][in_port])
        for src in self.rate_allocated[dpid][in_port]:
            if prev_allocated.get(src, 0) != self.rate_allocated[dpid][in_port][src]:
                rate    = self.rate_allocated[dpid][in_port][src]
                match   = parser.OFPMatch(in_port=self.mac_to_port[dpid][src], eth_src=src)
                self._mod_meter_entry(datapath, cmd, self.src_to_meter[dpid][src], rate)

    def _mod_meter_entry(self, dp, cmd, meter_id, rate, burst_size = 0):
        flags = dp.ofproto.OFPMF_KBPS
        bands = [dp.ofproto_parser.OFPMeterBandDrop(rate, burst_size)]
        meter_mod = dp.ofproto_parser.OFPMeterMod(dp, cmd, flags, meter_id, bands)
        dp.send_msg(meter_mod)

    def _add_flow(self, datapath, priority, match, actions, table, idle_to=0, buffer_id=None):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS,
                                             actions)]
        if buffer_id:
            mod = parser.OFPFlowMod(datapath=datapath, buffer_id=buffer_id,
                                    priority=priority, match=match,
                                    instructions=inst, table_id=table,
                                    idle_timeout = idle_to)
        else:
            mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                    match=match, instructions=inst, table_id=table,
                                    idle_timeout = idle_to)
        datapath.send_msg(mod)

    def _add_qos(self, datapath, priority, match, meter_id, rate, idle_to=0):
        self._add_qos_meter(datapath, meter_id, rate)
        self._add_qos_flow(datapath, priority, match, meter_id, idle_to)
        self.logger.debug('qos added')

    def _add_qos_meter(self, datapath, meter_id, rate):
        ofproto = datapath.ofproto
        cmd     = ofproto.OFPMC_ADD
        self._mod_meter_entry(datapath, cmd, meter_id, rate)

    def _add_qos_flow(self, datapath, priority, match, meter_id, idle_to):
        ofproto = datapath.ofproto
        parser  = datapath.ofproto_parser
        inst    = [parser.OFPInstructionMeter(meter_id), parser.OFPInstructionGotoTable(1)]
        mod     = parser.OFPFlowMod(datapath=datapath, priority=priority, match=match, 
                                    instructions=inst, table_id=0, idle_timeout=idle_to)
        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        # If you hit this you might want to increase
        # the "miss_send_length" of your switch
        if ev.msg.msg_len < ev.msg.total_len:
            self.logger.debug("packet truncated: only %s of %s bytes",
                              ev.msg.msg_len, ev.msg.total_len)
        msg      = ev.msg
        datapath = msg.datapath
        dpid     = datapath.id
        ofproto  = datapath.ofproto
        parser   = datapath.ofproto_parser
        in_port  = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]

        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            # ignore lldp packet
            return
        dst = eth.dst
        src = eth.src

        self.rate_request[dpid].setdefault(in_port, {})
        self.logger.info("packet in %s %s %s %s", dpid, src, dst, in_port)

        # search if it is a new in_port
        if in_port not in self.port_to_meter[dpid]:
            # add in_port's default meter
            self.logger.debug('adding qos to port: %s', in_port)
            self.n_meter[dpid] += 1
            self.port_to_meter[dpid][in_port] = self.n_meter[dpid]
            match   = parser.OFPMatch(in_port=in_port)
            # run thread to avoid performance decreasing
            hub.spawn(self._add_qos, datapath, 1, match, self.n_meter[dpid], self.default_rate)

        # search if there is a rule for the src
        if src in self.subs:
            # search if there is a existing meter already
            if src not in self.src_to_meter[dpid]:
                # thread = threading.Thread(target=self._new_sub, args=(datapath, src, in_port, ))
                # thread.start()
                hub.spawn(self._new_sub, datapath, src, in_port )

        # learn a mac address to avoid FLOOD next time.
        self.mac_to_port[dpid][src] = in_port

        if dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst]
        else:
            out_port = ofproto.OFPP_FLOOD

        actions = [parser.OFPActionOutput(out_port)]

        # install a flow to avoid packet_in next time
        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=dst)
            # verify if we have a valid buffer_id, if yes avoid to send both
            # flow_mod & packet_out
            if msg.buffer_id != ofproto.OFP_NO_BUFFER:
                self._add_flow(datapath, 1, match, actions, 1, idle_to=30, buffer_id=msg.buffer_id)
                return
            else:
                self._add_flow(datapath, 1, match, actions, 1, idle_to=30)
        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data

        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                  in_port=in_port, actions=actions, data=data)

        datapath.send_msg(out)

    def _new_sub(self, datapath, src, in_port):
        ofproto = datapath.ofproto
        parser  = datapath.ofproto_parser
        dpid    = datapath.id
        # recalculate rate allocated to in_port
        self.rate_request[dpid][in_port][src] = int(self.subs[src])
        prev_allocated = self.rate_allocated[dpid].get(in_port, {})
        self.rate_allocated[dpid][in_port] = self._rate_control(self.max_rate, self.rate_request[dpid][in_port], self.rate_used_mod[dpid].get(in_port, {}))
        # add meter and flow
        self.logger.debug('adding qos to src: %s', src)
        self.n_meter[dpid] += 1
        self.src_to_meter[dpid][src] = self.n_meter[dpid]
        self.meter_to_src[dpid][self.n_meter[dpid]] = src
        rate    = self.rate_allocated[dpid][in_port][src]
        match   = parser.OFPMatch(in_port=in_port, eth_src=src)
        self._add_qos(datapath, 2, match, self.n_meter[dpid], rate)

        # modify the others in_port's meters 
        for src2 in self.rate_allocated[dpid][in_port]:
            if src != src2 and  prev_allocated.get(src2, 0) != self.rate_allocated[dpid][in_port][src2]:
                self.logger.debug('modifying qos to src: %s', src2)
                cmd     = ofproto.OFPMC_MODIFY
                rate    = self.rate_allocated[dpid][in_port][src2]
                match   = parser.OFPMatch(in_port=self.mac_to_port[dpid][src2], eth_src=src2)
                self._mod_meter_entry(datapath, cmd, self.src_to_meter[dpid][src2], rate)

    def _rate_control(self, bandwith, requested, used):
        allocated = {}
        totalRequested = sum(requested.values())
        totalUsed = sum(used.values())
        partOfWhole = 0
        leftOver = 0
        if totalRequested < bandwith:
            allocated = requested
            leftOver = bandwith - totalRequested
        else:
            requested_base = requested
            requested = requested.copy()
            for src in requested:
                tmp = int((used.get(src, requested[src]*0.5)*1.5))
                if tmp < requested[src]:
                    requested[src] = tmp
                if requested[src] < 5000:
                    requested[src] = 5000
            partOfWhole = int(bandwith/len(requested))
            leftOver = bandwith % len(requested)
            for src in requested:
                if partOfWhole > requested[src]:
                    allocated[src] = requested[src]
                    leftOver += partOfWhole - requested[src]
                else:
                    allocated[src] = partOfWhole
            while leftOver > 0:
                stillNeed = 0
                for src in requested:
                    if (requested_base[src] - allocated[src]) > 0:
                        stillNeed += 1
                if stillNeed < leftOver:
                    for src in requested:
                        if (requested_base[src] - allocated[src]) > 0:
                             allocated[src]+=1
                             leftOver-=1
                else:
                    maxDiff = 0
                    tempI = None
                    for src in requested:
                        if requested[src] - allocated[src] >= maxDiff:
                            maxDiff = requested[src] - allocated[src]
                            tempI = src
                            self.logger.debug('SRC: %s', tempI)
                    allocated[tempI] += 1
                    leftOver -= 1
        return allocated
