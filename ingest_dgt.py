import os
import re
import pandas as pd
import geopandas as gpd
import asyncpg
import asyncio
from dotenv import load_dotenv

load_dotenv()

DB_DSN = os.getenv("DB_DSN", "postgresql://postgres:postgrespassword@localhost:5432/dgt_risk_db")

W_FALLECIDOS = 10.0
W_HERIDOS_GRAVES = 3.0
W_HERIDOS_LEVES = 1.0

def calcular_peso_severidad(row):
    return (row.get('TOTAL_MU30DF', 0) * W_FALLECIDOS) + \
           (row.get('TOTAL_HG30DF', 0) * W_HERIDOS_GRAVES) + \
           (row.get('TOTAL_HL30DF', 0) * W_HERIDOS_LEVES)

def normalizar_carretera(val):
    if pd.isna(val):
        return ""
    s = str(val).strip().upper()
    return re.sub(r'[^A-Z0-9]', '', s)

async def cargar_microdatos_dgt_anio(csv_path: str, gpkg_path: str, anio: int, pk_map: dict = None):
    print(f"\n📂 Processing {csv_path} (Año {anio})...")
    df = pd.read_csv(csv_path, sep=';', low_memory=False)
    
    df = df.dropna(subset=['CARRETERA', 'KM'])
    df['CARRETERA_NORM'] = df['CARRETERA'].apply(normalizar_carretera)
    df['KM'] = pd.to_numeric(df['KM'], errors='coerce')
    df = df.dropna(subset=['KM'])
    df['PESO_SEVERIDAD'] = df.apply(calcular_peso_severidad, axis=1)

    # Cargar la capa de PKs en memoria solo si no se ha cargado previamente
    if pk_map is None:
        print("📍 Cargando capa 'rt_ppkk_p' del GeoPackage IGN en memoria...")
        pks_gdf = gpd.read_file(gpkg_path, layer="rt_ppkk_p", engine="pyogrio")
        if pks_gdf.crs and pks_gdf.crs.to_epsg() != 4326:
            pks_gdf = pks_gdf.to_crs(epsg=4326)

        pks_gdf['carretera_norm'] = pks_gdf['nombre'].apply(normalizar_carretera)
        pks_gdf['pk_num'] = pd.to_numeric(pks_gdf['numero'], errors='coerce')

        pk_map = {}
        for _, row in pks_gdf.iterrows():
            key = (row['carretera_norm'], float(row['pk_num']))
            if key not in pk_map and row.geometry is not None:
                pk_map[key] = (row.geometry.x, row.geometry.y)
        print(f"🗺️ Base de PKs lista con {len(pk_map)} hito(s).")

    accidentes_a_insertar = []
    encontrados = 0

    for _, row in df.iterrows():
        cod_norm = row['CARRETERA_NORM']
        pk = float(row['KM'])
        fallecidos = int(row.get('TOTAL_MU30DF', 0))
        graves = int(row.get('TOTAL_HG30DF', 0))
        leves = int(row.get('TOTAL_HL30DF', 0))
        peso = float(row['PESO_SEVERIDAD'])

        key = (cod_norm, pk)
        if key in pk_map:
            lon, lat = pk_map[key]
            accidentes_a_insertar.append((
                row['CARRETERA'], pk, fallecidos, graves, leves, peso, lon, lat, anio
            ))
            encontrados += 1

    print(f"  └─ Geolocalizados {encontrados} de {len(df)} accidentes.")

    conn = await asyncpg.connect(DB_DSN)

    # 🛠️ AÚN NO EXISTÍA LA COLUMNA: Asegurar esquema de la tabla
    await conn.execute("""
        ALTER TABLE accidentes ADD COLUMN IF NOT EXISTS anio INTEGER;
        CREATE INDEX IF NOT EXISTS idx_accidentes_anio ON accidentes(anio);
    """)

    # Limpiamos solo los datos del año actual antes de reinsertar
    await conn.execute("DELETE FROM accidentes WHERE anio = $1 OR anio IS NULL;", anio)

    insert_query = """
        INSERT INTO accidentes (
            codigo_carretera, pk, fallecidos_30d, heridos_graves, heridos_leves, peso_severidad, geom, anio
        ) VALUES (
            $1, $2, $3, $4, $5, $6, ST_SetSRID(ST_MakePoint($7, $8), 4326), $9
        );
    """
    await conn.executemany(insert_query, accidentes_a_insertar)
    await conn.close()
    print(f"🚀 ¡Ingesta del año {anio} completada!")
    
    return pk_map

async def main():
    gpkg_path = "geopackage/rt_viaria.gpkg"
    
    # Ajusta los nombres de tus archivos CSV históricos aquí
    archivos_historicos = {
        2020: "TABLA_ACCIDENTES_20.csv",
        2021: "TABLA_ACCIDENTES_21.csv",
        2022: "TABLA_ACCIDENTES_22.csv",
        2023: "TABLA_ACCIDENTES_23.csv",
        2024: "TABLA_ACCIDENTES_24.csv",
    }

    pk_map_cached = None
    for anio, path in archivos_historicos.items():
        if os.path.exists(path):
            pk_map_cached = await cargar_microdatos_dgt_anio(path, gpkg_path, anio, pk_map_cached)
        else:
            print(f"⚠️ Aviso: No se encontró el archivo {path}, omitiendo año {anio}.")

if __name__ == "__main__":
    asyncio.run(main())