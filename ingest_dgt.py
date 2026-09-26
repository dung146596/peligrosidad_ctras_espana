import os
import re
import json
import pandas as pd
import geopandas as gpd
import asyncpg
import asyncio
import requests
from dotenv import load_dotenv
from shapely.geometry import LineString, MultiLineString
from shapely.ops import linemerge, unary_union

load_dotenv()

DB_DSN = os.getenv(
    "DB_DSN",
    "postgresql://postgres:postgrespassword@localhost:5432/dgt_risk_db"
)

W_FALLECIDOS = 10.0
W_HERIDOS_GRAVES = 3.0
W_HERIDOS_LEVES = 1.0
TOLERANCIA_PK_KM = 10
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OSM_CACHE_PATH = "geopackage/osm_fallback_cache.json"
OSM_FALLBACK_ENABLED = os.getenv("OSM_FALLBACK_ENABLED", "0").lower() in {"1", "true", "yes"}

PROV_BBOX = {
    '09': (41.8, -4.7, 43.3, -2.8),
    '11': (35.8, -6.6, 36.8, -5.1),
    '15': (42.7, -9.3, 43.8, -6.5),
    '27': (41.8, -7.1, 43.8, -6.5),
    '28': (39.8, -4.6, 41.2, -3.0),  # Madrid
    '32': (41.8, -8.4, 42.5, -6.7),
    '33': (42.8, -7.2, 43.7, -4.4),
    '35': (27.5, -15.9, 28.3, -13.3), # Las Palmas
    '36': (41.8, -8.9, 42.2, -8.3),
    '39': (43.0, -4.9, 43.6, -3.2),
    '44': (39.7, -1.6, 41.3, 0.3),   # Teruel
    '45': (39.2, -5.5, 40.3, -3.0),  # Toledo
}

# Mapeo de códigos INE de provincia a prefijos de carreteras autonómicas/provinciales
PROV_PREFIJOS = {
    '33': ['AS', 'O'],        # Asturias
    '09': ['BU', 'CL'],       # Burgos
    '11': ['CA', 'A'],        # Cádiz
    '15': ['AC', 'DP'],       # A Coruña
    '27': ['LU'],             # Lugo
    '28': ['M', 'M30', 'M40'], # Madrid
    '32': ['OU'],             # Ourense
    '35': ['GC', 'LZ', 'FVT'],# Las Palmas (Gran Canaria, Lanzarote, Fuerteventura)
    '36': ['PO'],             # Pontevedra
    '39': ['CA', 'S'],        # Cantabria
    '44': ['TE'],             # Teruel
    '45': ['TO', 'CM'],       # Toledo / Castilla-La Mancha
}


def calcular_peso_severidad(row):
    return (
        row.get('TOTAL_MU30DF', 0) * W_FALLECIDOS
        + row.get('TOTAL_HG30DF', 0) * W_HERIDOS_GRAVES
        + row.get('TOTAL_HL30DF', 0) * W_HERIDOS_LEVES
    )


def normalizar_carretera(val):
    if pd.isna(val) or not val:
        return ""
    s = str(val).replace("-", "").replace(" ", "").strip().upper()
    limpio = re.sub(r'[^A-Z0-9]', '', s)
    # Convertir formatos como AS014 a AS14 o M030 a M30
    return re.sub(r'([A-Z]+)0*(\d+)', r'\1\2', limpio)


def candidatos_carretera(carretera, provincia=None):
    """Devuelve alias sin duplicar el prefijo de la carretera."""
    carretera_norm = normalizar_carretera(carretera)
    if not carretera_norm:
        return set()

    candidatos = {carretera_norm}
    provincia_norm = str(provincia).split('.')[0].zfill(2) if provincia is not None else ''
    for prefijo in PROV_PREFIJOS.get(provincia_norm, []):
        if carretera_norm.startswith(prefijo):
            continue
        if carretera_norm[0].isdigit():
            candidatos.add(f'{prefijo}{carretera_norm}')
    return candidatos


def limpiar_pk(val):
    try:
        val_str = str(val).replace(",", ".").strip()
        match = re.search(r'\d+(\.\d+)?', val_str)
        if match:
            return round(float(match.group(0)))
        return None
    except (ValueError, TypeError):
        return None


def cargar_cache_osm():
    if not os.path.exists(OSM_CACHE_PATH):
        return {}
    try:
        with open(OSM_CACHE_PATH, 'r', encoding='utf-8') as cache_file:
            return json.load(cache_file)
    except (OSError, json.JSONDecodeError):
        return {}


def guardar_cache_osm(cache):
    os.makedirs(os.path.dirname(OSM_CACHE_PATH), exist_ok=True)
    with open(OSM_CACHE_PATH, 'w', encoding='utf-8') as cache_file:
        json.dump(cache, cache_file, ensure_ascii=True)


def referencia_osm(carretera):
    """Convierte A480/N632 en referencias habituales de OSM (A-480/N-632)."""
    carretera_norm = normalizar_carretera(carretera)
    match = re.fullmatch(r'([A-Z]+)(\d+)', carretera_norm)
    if not match:
        return None
    return f'{match.group(1)}-{int(match.group(2))}'


def obtener_linea_osm(carretera, provincia, cache, permitir_red=True):
    provincia_norm = str(provincia).split('.')[0].zfill(2)
    referencia = referencia_osm(carretera)
    bbox = PROV_BBOX.get(provincia_norm)
    if not referencia or not bbox:
        return None

    cache_key = f'{provincia_norm}:{referencia}'
    if cache_key in cache:
        coords = cache[cache_key]
        return LineString(coords) if coords else None
    if not permitir_red:
        return None

    south, west, north, east = bbox
    query = (
        '[out:json][timeout:60];'
        f'way["ref"="{referencia}"]({south},{west},{north},{east});'
        'out geom;'
    )

    try:
        response = requests.post(
            OVERPASS_URL,
            data=query,
            headers={'User-Agent': 'dgt-mapa-riesgo/1.0 (educational project)'},
            timeout=90,
        )
        response.raise_for_status()
        elements = response.json().get('elements', [])
        lines = [
            LineString([(point['lon'], point['lat']) for point in element.get('geometry', [])])
            for element in elements
            if len(element.get('geometry', [])) >= 2
        ]
        if not lines:
            cache[cache_key] = None
            guardar_cache_osm(cache)
            return None

        merged = linemerge(unary_union(lines))
        if isinstance(merged, MultiLineString):
            merged = max(merged.geoms, key=lambda line: line.length)
        cache[cache_key] = list(merged.coords)
        guardar_cache_osm(cache)
        return merged
    except (requests.RequestException, ValueError, KeyError):
        print(f"  ⚠️ No se pudo consultar Overpass para {referencia} ({provincia_norm}).")
        return None


def punto_osm_para_pk(linea, pk, pk_min, pk_max):
    if not linea:
        return None
    if pk_max <= pk_min:
        fraccion = 0.5
    else:
        fraccion = (pk - pk_min) / (pk_max - pk_min)
    fraccion = max(0.0, min(1.0, fraccion))
    punto = linea.interpolate(fraccion, normalized=True)
    return punto.x, punto.y


async def cargar_microdatos_dgt_anio(
    csv_path: str,
    gpkg_dir: str,
    anio: int,
    pk_data_cache: tuple = None
):
    print(f"\n📂 Processing {csv_path} (Año {anio})...")

    try:
        df = pd.read_csv(csv_path, sep=';', low_memory=False, encoding='utf-8')
    except UnicodeDecodeError:
        df = pd.read_csv(csv_path, sep=';', low_memory=False, encoding='latin1')

    col_km = next((c for c in df.columns if c.upper() in ['KM', 'PK', 'PUNTO_KILOMETRICO']), None)
    if not col_km:
        print(f"⚠️ No se encontró columna de PK en {csv_path}")
        return pk_data_cache

    df = df.dropna(subset=['CARRETERA', col_km])
    df['CARRETERA_NORM'] = df['CARRETERA'].apply(normalizar_carretera)
    df['KM'] = df[col_km].apply(limpiar_pk)
    df = df.dropna(subset=['KM'])

    df['PESO_SEVERIDAD'] = df.apply(calcular_peso_severidad, axis=1)

    # 1. Cargar dinámicamente TODOS los archivos .gpkg de la carpeta 'geopackage/'
    if pk_data_cache is None:
        pk_map = {}
        vial_pks = {}

        if not os.path.exists(gpkg_dir):
            print(f"❌ La carpeta '{gpkg_dir}' no existe.")
            return None

        # Escanear todos los ficheros .gpkg
        archivos_gpkg = [
            os.path.join(gpkg_dir, f) for f in os.listdir(gpkg_dir)
            if f.lower().endswith('.gpkg')
        ]

        print(f"🔍 Detectados {len(archivos_gpkg)} archivos GeoPackage en '{gpkg_dir}'.")

        for fuente_path in archivos_gpkg:
            try:
                capas_disponibles = gpd.list_layers(fuente_path)['name'].tolist()
            except Exception as e:
                print(f"⚠️ No se pudieron leer las capas de {fuente_path}: {e}")
                continue

            # Buscar capas que contengan puntos kilométricos
            capas_pk = [c for c in capas_disponibles if any(term in c.lower() for term in ['ppkk', 'portalpk', 'puntos_k', 'pk'])]

            for capa in capas_pk:
                print(f"📍 Cargando capa '{capa}' de {os.path.basename(fuente_path)}...")
                try:
                    pks_gdf = gpd.read_file(fuente_path, layer=capa, engine="pyogrio")
                except Exception as e:
                    print(f"⚠️ Error al leer capa '{capa}' en {fuente_path}: {e}")
                    continue

                if pks_gdf.crs and pks_gdf.crs.to_epsg() != 4326:
                    pks_gdf = pks_gdf.to_crs(epsg=4326)

                # Identificar nombres de columnas dinámicamente
                col_nombre = next((c for c in pks_gdf.columns if c.lower() in ['nombre', 'carr_nom', 'carretera', 'via']), None)
                col_rotulo = next((c for c in pks_gdf.columns if c.lower() in ['rotulo', 'denominacion', 'codigo', 'ref']), None)
                col_numero = next((c for c in pks_gdf.columns if c.lower() in ['numero', 'pk', 'pk_num', 'km']), None)

                pks_gdf['carr_nombre'] = pks_gdf[col_nombre].fillna('').apply(normalizar_carretera) if col_nombre else ""
                pks_gdf['carr_rotulo'] = pks_gdf[col_rotulo].fillna('').apply(normalizar_carretera) if col_rotulo else ""
                pks_gdf['pk_num'] = pks_gdf[col_numero].apply(limpiar_pk) if col_numero else None

                for _, row in pks_gdf.iterrows():
                    if pd.isna(row['pk_num']) or row.geometry is None:
                        continue

                    pk = int(row['pk_num'])
                    coords = (row.geometry.x, row.geometry.y)

                    nombres_posibles = set(filter(None, [row['carr_nombre'], row['carr_rotulo']]))

                    for v in nombres_posibles:
                        pk_map[(v, pk)] = coords
                        if v not in vial_pks:
                            vial_pks[v] = {}
                        if pk not in vial_pks[v]:
                            vial_pks[v][pk] = coords

        print(f"🗺️ Base de PKs unificada lista con {len(pk_map)} combinación(es) directa(s).")
        pk_data_cache = (pk_map, vial_pks, cargar_cache_osm())

    pk_map, vial_pks, osm_cache = pk_data_cache

    accidentes_a_insertar = []
    carreteras_no_geolocalizadas = {}
    encontrados = 0

    col_prov = next((c for c in df.columns if c.upper() in ['PROVINCIA', 'COD_PROVINCIA', 'ID_PROVINCIA']), None)
    rangos_pk = {}
    for _, accidente in df.iterrows():
        carretera = str(accidente['CARRETERA']).strip().upper()
        if carretera == 'NO INVENTARIADA' or not col_prov:
            continue
        provincia = str(accidente[col_prov]).split('.')[0].zfill(2)
        pk_value = accidente['KM']
        if pd.isna(pk_value):
            continue
        clave = (provincia, normalizar_carretera(carretera))
        if clave not in rangos_pk:
            rangos_pk[clave] = [float(pk_value), float(pk_value)]
        else:
            rangos_pk[clave][0] = min(rangos_pk[clave][0], float(pk_value))
            rangos_pk[clave][1] = max(rangos_pk[clave][1], float(pk_value))

    for _, row in df.iterrows():
        cod_norm = row['CARRETERA_NORM']
        pk = int(row['KM'])
        provincia = row[col_prov] if col_prov else None
        candidatos = candidatos_carretera(row['CARRETERA'], provincia)

        fallecidos = int(row.get('TOTAL_MU30DF', 0))
        graves = int(row.get('TOTAL_HG30DF', 0))
        leves = int(row.get('TOTAL_HL30DF', 0))
        peso = float(row['PESO_SEVERIDAD'])
        tipo_acc = str(row.get('TIPO_ACCIDENTE', row.get('TIPO_ACCID_PADRE', 'Desconocido'))).strip()

        coords = None

        # Estrategia 1: Coincidencia exacta con cualquier alias
        for candidato in candidatos:
            if (candidato, pk) in pk_map:
                coords = pk_map[(candidato, pk)]
                break

        # Estrategia 2: Usar el PK disponible más cercano
        if not coords:
            mejores_opciones = []
            for candidato in candidatos:
                if candidato not in vial_pks:
                    continue
                pk_cercano = min(vial_pks[candidato], key=lambda valor: abs(valor - pk))
                distancia = abs(pk_cercano - pk)
                if distancia <= TOLERANCIA_PK_KM:
                    mejores_opciones.append((distancia, vial_pks[candidato][pk_cercano]))
            if mejores_opciones:
                coords = min(mejores_opciones, key=lambda opcion: opcion[0])[1]

        # Estrategia 3: Geometría OSM de respaldo
        if not coords and cod_norm != 'NOINVENTARIADA' and provincia is not None:
            provincia_norm = str(provincia).split('.')[0].zfill(2)
            rango = rangos_pk.get((provincia_norm, cod_norm))
            if rango:
                linea_osm = obtener_linea_osm(
                    row['CARRETERA'],
                    provincia_norm,
                    osm_cache,
                    permitir_red=OSM_FALLBACK_ENABLED,
                )
                coords = punto_osm_para_pk(linea_osm, float(row['KM']), rango[0], rango[1])
                if coords:
                    print(f"  🌐 Geometría OSM aproximada: {row['CARRETERA']} ({provincia_norm})")

        if coords:
            lon, lat = coords
            accidentes_a_insertar.append((
                row['CARRETERA'],
                pk,
                fallecidos,
                graves,
                leves,
                peso,
                lon,
                lat,
                anio,
                tipo_acc
            ))
            encontrados += 1
        else:
            carretera = str(row['CARRETERA']).strip()
            carreteras_no_geolocalizadas[carretera] = carreteras_no_geolocalizadas.get(carretera, 0) + 1

    print(f"  └─ Geolocalizados {encontrados} de {len(df)} accidentes.")
    if carreteras_no_geolocalizadas:
        resumen = sorted(carreteras_no_geolocalizadas.items(), key=lambda item: item[1], reverse=True)[:10]
        print(f"  └─ Sin geometría IGN/Local (top 10): {resumen}")

    conn = await asyncpg.connect(DB_DSN)

    await conn.execute("""
        ALTER TABLE accidentes ADD COLUMN IF NOT EXISTS anio INTEGER;
        ALTER TABLE accidentes ADD COLUMN IF NOT EXISTS tipo_accidente VARCHAR(100);
        CREATE INDEX IF NOT EXISTS idx_accidentes_anio ON accidentes(anio);
    """)

    await conn.execute("DELETE FROM accidentes WHERE anio = $1;", anio)

    insert_query = """
        INSERT INTO accidentes (
            codigo_carretera, pk, fallecidos_30d, heridos_graves, heridos_leves,
            peso_severidad, geom, anio, tipo_accidente
        ) VALUES (
            $1, $2, $3, $4, $5, $6, ST_SetSRID(ST_MakePoint($7, $8), 4326), $9, $10
        );
    """

    await conn.executemany(insert_query, accidentes_a_insertar)
    await conn.close()

    print(f"🚀 ¡Ingesta del año {anio} completada!")
    return pk_data_cache


async def main():
    gpkg_dir = "geopackage"

    archivos_historicos = {
        2020: "TABLA_ACCIDENTES_20.csv",
        2021: "TABLA_ACCIDENTES_21.csv",
        2022: "TABLA_ACCIDENTES_22.csv",
        2023: "TABLA_ACCIDENTES_23.csv",
        2024: "TABLA_ACCIDENTES_24.csv",
    }

    pk_cache = None

    for anio, path in archivos_historicos.items():
        if os.path.exists(path):
            pk_cache = await cargar_microdatos_dgt_anio(
                path,
                gpkg_dir,
                anio,
                pk_cache
            )
        else:
            print(f"⚠️ Aviso: No se encontró {path}, omitiendo año {anio}.")


if __name__ == "__main__":
    asyncio.run(main())