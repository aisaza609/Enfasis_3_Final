from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, CONFIG_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, arp, ipv4
from ryu.lib.packet import ether_types
from ryu.controller.handler import DEAD_DISPATCHER
import requests
import psycopg2.extras


class ControladorFinal(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def _init_(self, *args, **kwargs):
        super(ControladorFinal, self)._init_(*args, **kwargs)
        self.mac_to_port = {}
        self.host_to_switch_map = {}  # mac -> {dpid, port, ip, name}
        self.host_ip_to_mac = {}
        self.host_mac_to_ip = {}
        self.datapaths = {}
        self._load_topology_from_db()

    def _load_topology_from_db(self):
        try:
            conn = psycopg2.connect(
                dbname="geant_network",
                user="geant_user",
                password="geant",
                host="192.168.1.13",
                port="5432"
            )
            cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
            cur.execute("SELECT nombre, switch_asociado, ipv4 AS ip, mac FROM hosts;")
            hosts = cur.fetchall()
            for h in hosts:
                nombre = h['nombre']
                ip = h['ip']
                mac = h['mac']
                dpid = int("{:016x}".format(h['switch_asociado']), 16)

                cur.execute("""
                    SELECT puerto_origen FROM puertos
                    WHERE nodo_origen = (SELECT nombre FROM switches WHERE id_switch = %s)
                      AND nodo_destino = %s
                    LIMIT 1;
                """, (h['switch_asociado'], nombre))
                puerto = cur.fetchone()
                puerto = puerto[0] if puerto else 1

                self.host_to_switch_map[mac] = {
                    "dpid": dpid,
                    "port": puerto,
                    "ip": ip,
                    "name": nombre
                }
                self.host_mac_to_ip[mac] = ip
                self.host_ip_to_mac[ip] = mac
        except Exception as e:
            self.logger.error(f"Error al cargar topología desde BD: {e}")
        finally:
            cur.close()
            conn.close()

    def add_flow(self, datapath, priority, match, actions, idle_timeout=60, hard_timeout=0):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                match=match, instructions=inst,
                                idle_timeout=idle_timeout,
                                hard_timeout=hard_timeout)
        datapath.send_msg(mod)

    def _send_packet_out(self, datapath, buffer_id, in_port, actions, data):
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        out = parser.OFPPacketOut(datapath=datapath, buffer_id=buffer_id,
                                  in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, CONFIG_DISPATCHER])
    def _state_change_handler(self, ev):
        datapath = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            if datapath.id not in self.datapaths:
                self.datapaths[datapath.id] = datapath
                self.logger.info(f"Switch conectado: {datapath.id:016x}")

                # Instalar regla table-miss
                parser = datapath.ofproto_parser
                ofproto = datapath.ofproto
                match = parser.OFPMatch()
                actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
                self.add_flow(datapath, 0, match, actions)

        elif ev.state == DEAD_DISPATCHER:
            if datapath.id in self.datapaths:
                del self.datapaths[datapath.id]
                self.logger.info(f"Switch desconectado: {datapath.id:016x}")

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        
        msg = ev.msg
        datapath = msg.datapath
        dpid = datapath.id
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth.ethertype in [ether_types.ETH_TYPE_LLDP, ether_types.ETH_TYPE_IPV6]:
            return
        self.logger.info(f"[RECEIVED] PacketIn from switch {dpid} in_port={in_port}, src={eth.src}, dst={eth.dst}")
        src_mac = eth.src
        dst_mac = eth.dst

        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][src_mac] = in_port

        if eth.ethertype == ether_types.ETH_TYPE_ARP:
            arp_pkt = pkt.get_protocol(arp.arp)
            if arp_pkt and arp_pkt.opcode == arp.ARP_REQUEST:
                dst_ip = arp_pkt.dst_ip
                src_ip = arp_pkt.src_ip
                target_mac = self.host_ip_to_mac.get(dst_ip)
                if target_mac:
                    self.logger.info(f"[ARP PROXY] Respondiendo: {dst_ip} → {target_mac}")
                    src_mac = target_mac
                    eth_reply = ethernet.ethernet(dst=eth.src, src=src_mac, ethertype=ether_types.ETH_TYPE_ARP)
                    arp_reply = arp.arp(opcode=arp.ARP_REPLY,
                                       src_mac=src_mac, src_ip=dst_ip,
                                       dst_mac=eth.src, dst_ip=src_ip)

                    pkt_out = packet.Packet()
                    pkt_out.add_protocol(eth_reply)
                    pkt_out.add_protocol(arp_reply)
                    pkt_out.serialize()

                    actions = [parser.OFPActionOutput(in_port)]
                    out = parser.OFPPacketOut(
                        datapath=datapath,
                        buffer_id=ofproto.OFP_NO_BUFFER,
                        in_port=ofproto.OFPP_CONTROLLER,
                        actions=actions,
                        data=pkt_out.data
                    )
                    datapath.send_msg(out)
                    return
                   
                  
        ip_src = self.host_mac_to_ip.get(src_mac)
        ip_dst = self.host_mac_to_ip.get(dst_mac)
        if not ip_src or not ip_dst:
            return

        host_origen = self.host_to_switch_map[src_mac]['name']
        host_destino = self.host_to_switch_map[dst_mac]['name']

        try:
            url = "http://192.168.1.13/ruta-con-puertos"
            data = {"host_origen": host_origen, "host_destino": host_destino}
            res = requests.post(url, json=data, timeout=3)
            ruta_ida = res.json().get("path", [])
        except Exception as e:
            self.logger.error(f"Error ruta ida: {e}")
            return

        for salto in ruta_ida:
            dpid_s = salto["dpid"]
            out_port = salto["out_port"]
            dp = self.datapaths.get(dpid_s)
            self.logger.info(f"[FLOW INSTALLED] {src_mac} → {dst_mac} en dpid {dpid_s} out_port={out_port}")
            if not dp:
                continue
            match = parser.OFPMatch(eth_src=src_mac, eth_dst=dst_mac)
            actions = [parser.OFPActionOutput(out_port)]
            self.add_flow(dp, 100, match, actions)
            
        try:
            url = "http://192.168.1.13/ruta-con-puertos-inversa"
            data = {"host_origen": host_origen, "host_destino": host_destino}
            res = requests.post(url, json=data, timeout=3)
            ruta_ret = res.json().get("path", [])
            
        except Exception as e:
            self.logger.error(f"Error ruta retorno: {e}")
            return

        for salto in ruta_ret:
            dpid_s = salto["dpid"]
            out_port = salto["out_port"]
            dp = self.datapaths.get(dpid_s)
            self.logger.info(f"[FLOW INSTALLED] {dst_mac} → {src_mac} en dpid {dpid_s} out_port={out_port}")
            if not dp:
                continue
            match = parser.OFPMatch(eth_src=dst_mac, eth_dst=src_mac)
            actions = [parser.OFPActionOutput(out_port)]
            self.add_flow(dp, 100, match, actions)
            

        if ruta_ida:
            primer_out = ruta_ida[0]["out_port"]
            actions = [parser.OFPActionOutput(primer_out)]
            data = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
            self._send_packet_out(datapath, msg.buffer_id, in_port, actions, data)