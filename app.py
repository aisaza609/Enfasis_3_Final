from flask import Flask, request, jsonify
import networkx as nx
import psycopg2

#si fall quita esto
from flask_cors import CORS
## codigo funcional con mi APPWEB

app = Flask(_name_)  # CORREGIDO
CORS(app)
# -----------------------
# CONSTRUIR GRAFO DESDE BD
# -----------------------
def construir_grafo_desde_bd():
    G = nx.Graph()

    conn = psycopg2.connect(
        dbname="geant_network",
        user="geant_user",
        password="geant",
        host="192.168.1.13",
        port="5432"
    )
    cur = conn.cursor()

    cur.execute("SELECT id_switch, nombre FROM switches;")
    for id_sw, nombre in cur.fetchall():
        G.add_node(nombre)

    cur.execute("SELECT id_origen, id_destino, ancho_banda FROM enlaces;")
    for id_origen, id_destino, bw in cur.fetchall():
        cur.execute("SELECT nombre FROM switches WHERE id_switch = %s", (id_origen,))
        origen = cur.fetchone()[0]
        cur.execute("SELECT nombre FROM switches WHERE id_switch = %s", (id_destino,))
        destino = cur.fetchone()[0]

        try:
            bw = float(bw)
            peso = 1 / bw
        except:
            bw = 100.0
            peso = 1 / bw

        G.add_edge(origen, destino, weight=peso, bw=bw)

    cur.close()
    conn.close()
    return G

grafo = construir_grafo_desde_bd()

# -----------------------
# ALGORITMOS DE RUTA
# -----------------------
def calcular_ruta(g, origen, destino, algoritmo):
    try:
        if algoritmo == "dijkstra":
            ruta = nx.dijkstra_path(g, source=origen, target=destino, weight='weight')
        else:
            ruta = nx.shortest_path(g, source=origen, target=destino)
        return ruta
    except nx.NetworkXNoPath:
        return []

# -----------------------
# BALANCEADOR DE CARGA
# -----------------------
servidores_rr = ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
rr_index = 0

servidores_wrr = [("10.0.0.1", 3), ("10.0.0.2", 1), ("10.0.0.3", 2)]
wrr_list = [ip for ip, peso in servidores_wrr for _ in range(peso)]
wrr_index = 0

def balancear(tipo):
    global rr_index, wrr_index
    if tipo == "wrr":
        ip = wrr_list[wrr_index]
        wrr_index = (wrr_index + 1) % len(wrr_list)
    else:
        ip = servidores_rr[rr_index]
        rr_index = (rr_index + 1) % len(servidores_rr)
    return ip

# -----------------------
# ENDPOINTS DE LA API
# -----------------------

@app.route('/ruta', methods=['POST'])
def ruta():
    datos = request.json
    origen = datos.get('origen')
    destino = datos.get('destino')
    algoritmo = datos.get('algoritmo', 'shortest')

    if not origen or not destino:
        return jsonify({"error": "Origen y destino son requeridos"}), 400

    resultado = calcular_ruta(grafo, origen, destino, algoritmo)
    return jsonify({"ruta": resultado})

@app.route('/balancear', methods=['POST'])
def balanceo():
    datos = request.json
    tipo = datos.get('tipo', 'rr')  # por defecto round robin
    servidor = balancear(tipo)
    return jsonify({"servidor": servidor})

@app.route("/switch-de-host", methods=["GET"])
def switch_de_host():
    host = request.args.get("host")
    if not host:
        return jsonify({"error": "Debe proporcionar el nombre del host (ej: h1)"}), 400

    try:
        conn = psycopg2.connect(
            dbname="geant_network",
            user="geant_user",
            password="geant",
            host="192.168.1.13",
            port="5432"
        )
        cur = conn.cursor()
        query = """
            SELECT nodo_origen FROM puertos
            WHERE nodo_destino = %s AND nodo_origen LIKE 's%%'
            LIMIT 1;
        """
        cur.execute(query, (host,))
        result = cur.fetchone()
        cur.close()
        conn.close()
        if result:
            return jsonify({"switch": result[0]})
        else:
            return jsonify({"error": f"No se encontró un switch conectado a {host}"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500
        
        
@app.route('/instalar-ruta', methods=['POST'])
def instalar_ruta():
    datos = request.json
    host_origen = datos.get('host_origen')
    host_destino = datos.get('host_destino')
    algoritmo = datos.get('algoritmo', 'shortest')

    if not host_origen or not host_destino:
        return jsonify({"error": "Se requieren host_origen y host_destino"}), 400

    # 1. Obtener el switch al que está conectado cada host
    try:
        conn = psycopg2.connect(
            dbname="geant_network",
            user="geant_user",
            password="geant",
            host="192.168.1.13",
            port="5432"
        )
        cur = conn.cursor()
        cur.execute("SELECT nodo_origen FROM puertos WHERE nodo_destino = %s;", (host_origen,))
        sw_origen = cur.fetchone()
        cur.execute("SELECT nodo_origen FROM puertos WHERE nodo_destino = %s;", (host_destino,))
        sw_destino = cur.fetchone()
        cur.close()
        conn.close()
    except Exception as e:
        return jsonify({"error": f"Error consultando switches: {str(e)}"}), 500

    if not sw_origen or not sw_destino:
        return jsonify({"error": "No se encontraron switches para los hosts"}), 404

    sw_origen = sw_origen[0]
    sw_destino = sw_destino[0]

    # 2. Calcular ruta entre switches
    ruta_switches = calcular_ruta(grafo, sw_origen, sw_destino, algoritmo)
    if not ruta_switches:
        return jsonify({"error": "No se encontró ruta entre switches"}), 404

    return jsonify({
        "ruta": ruta_switches,
        "switch_origen": sw_origen,
        "switch_destino": sw_destino
    })

@app.route("/grafo-json")
def grafo_json():
    nodes = []
    edges = []

    # Conexión a la BD para obtener también los hosts
    conn = psycopg2.connect(
        dbname="geant_network",
        user="geant_user",
        password="geant",
        host="192.168.1.13",
        port="5432"
    )
    cur = conn.cursor()

    # Añadir switches al grafo
    for nodo in grafo.nodes():
        color = "#0077cc" if nodo.startswith("s") else "#66bb6a"
        nodes.append({"id": nodo, "label": nodo, "color": color})

    # Añadir enlaces entre switches
    for origen, destino, datos in grafo.edges(data=True):
        bw = datos.get("bw", 100)
        color = "#FF0000" if bw <= 10 else "#FFA500" if bw <= 100 else "#00AA00"
        edges.append({
            "from": origen,
            "to": destino,
            "color": {"color": color},
            "width": 2
        })

    # Consultar y añadir hosts conectados
    cur.execute("SELECT nodo_origen, nodo_destino FROM puertos WHERE nodo_destino LIKE 'h%';")
    for nodo_origen, nodo_destino in cur.fetchall():
        # Añadir nodo host si no está
        nodes.append({
            "id": nodo_destino,
            "label": nodo_destino,
            "color": "#cccccc"  # gris claro para hosts
        })
        # Añadir enlace entre switch y host
        edges.append({
            "from": nodo_origen,
            "to": nodo_destino,
            "color": {"color": "#888888"},  # gris medio para conexión host
            "width": 1
        })

    cur.close()
    conn.close()

    return jsonify({"nodes": nodes, "edges": edges})


# -----------------------
# EJECUCIÓN LOCAL (pruebas)
# -----------------------
if _name== 'main_':  # CORREGIDO
    app.run(host='0.0.0.0', port=5000)
@app.route('/hosts', methods=['GET'])
def obtener_hosts():
    hosts = [f"h{i}" for i in range(1, 47)]  # h1 a h46
    return jsonify({"hosts": hosts})