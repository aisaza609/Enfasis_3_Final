from mininet.topo import Topo
from mininet.link import TCLink
from mininet.node import OVSKernelSwitch
import psycopg2

class GeantTopo(Topo):
    def build(self):
        switches = {}
        hosts = {}
        puertos_dict = {}

        # Conectarse a la base de datos
        conn = psycopg2.connect(
            dbname="geant_network",
            user="geant_user", 
            password="geant" ,   # cambia si usas otro usuario
            host="192.168.1.13",
            port="5432"
        )
        cur = conn.cursor()

        # Leer switches
        cur.execute("SELECT id_switch, nombre, dpid FROM switches;")
        for id_switch, nombre, dpid in cur.fetchall():
            sw = self.addSwitch(nombre, dpid=dpid, cls=OVSKernelSwitch, stp=True)
            switches[nombre] = sw

        # Leer hosts
        cur.execute("SELECT nombre, ipv4, mac, switch_asociado, puerto_switch FROM hosts;")
        for nombre, ip, mac, switch_id, puerto in cur.fetchall():
            h = self.addHost(nombre, ip=str(ip), mac=mac)
            hosts[nombre] = h

            # Buscar nombre del switch asociado
            cur.execute("SELECT nombre FROM switches WHERE id_switch = %s;", (switch_id,))
            sw_nombre = cur.fetchone()[0]

            kwargs = {}
            if puerto:  # puerto explícito definido
                kwargs['port2'] = puerto
            self.addLink(h, switches[sw_nombre], **kwargs)

        # Leer tabla de puertos
        cur.execute("SELECT nodo_origen, nodo_destino, puerto_origen, puerto_destino FROM puertos;")
        for n1, n2, p1, p2 in cur.fetchall():
            puertos_dict[(n1, n2)] = (p1, p2)

        # Leer enlaces entre switches
        cur.execute("SELECT id_origen, id_destino, ancho_banda FROM enlaces;")
        for id1, id2, bw in cur.fetchall():
            # Convertir ancho de banda según indicación
            bw_reducido = float(bw) / 100 if bw else 10.0

            # Buscar nombres de switches
            cur.execute("SELECT nombre FROM switches WHERE id_switch = %s;", (id1,))
            sw1 = cur.fetchone()[0]
            cur.execute("SELECT nombre FROM switches WHERE id_switch = %s;", (id2,))
            sw2 = cur.fetchone()[0]

            # Verificar si hay puertos definidos
            p1, p2 = puertos_dict.get((sw1, sw2), (None, None))
            kwargs = {'cls': TCLink, 'bw': bw_reducido}
            if p1: kwargs['port1'] = p1
            if p2: kwargs['port2'] = p2

            self.addLink(switches[sw1], switches[sw2], **kwargs)

        # Cerrar conexión
        cur.close()
        conn.close()

# Registrar topología
topos = {'geant': (lambda: GeantTopo())}