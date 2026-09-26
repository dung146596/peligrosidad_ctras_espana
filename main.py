import os
import json
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import asyncpg
from typing import List
from dotenv import load_dotenv

load_dotenv()

DB_DSN = os.getenv("DB_DSN", "postgresql://postgres:postgrespassword@localhost:5432/dgt_risk_db")

# Mapeo oficial de códigos de tipo de accidente DGT
TIPO_ACCIDENTE_MAP = {
    "1": "Frontal",
    "2": "Fronto-lateral",
    "3": "Lateral",
    "4": "Por alcance",
    "5": "Múltiple o en caravana",
    "6": "Colisión contra obstáculo",
    "7": "Atropello a personas",
    "8": "Atropello a animales",
    "9": "Vuelco",
    "10": "Caída",
    "11": "Sólo salida de la vía",
    "12": "Salida por la izquierda con colisión",
    "13": "Salida por la izquierda con despeñamiento",
    "14": "Salida por la izquierda con vuelco",
    "15": "Salida por la izquierda (otro)",
    "16": "Salida por la derecha con colisión",
    "17": "Salida por la derecha con despeñamiento",
    "18": "Salida por la derecha con vuelco",
    "19": "Salida por la derecha (otro)",
    "20": "Otro tipo de accidente"
}

app = FastAPI(title="API de Riesgo DGT")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Servir archivos estáticos del frontend
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.on_event("startup")
async def init_db():
    """Crea la tabla de accidentes e índices en PostGIS si no existen."""
    conn = await asyncpg.connect(DB_DSN)
    
    await conn.execute("""
        CREATE EXTENSION IF NOT EXISTS postgis;
        
        CREATE TABLE IF NOT EXISTS accidentes (
            id SERIAL PRIMARY KEY,
            codigo_carretera VARCHAR(50),
            pk NUMERIC(6,2),
            fallecidos_30d INT DEFAULT 0,
            heridos_graves INT DEFAULT 0,
            heridos_leves INT DEFAULT 0,
            peso_severidad FLOAT,
            geom GEOMETRY(Point, 4326),
            anio INT,
            tipo_accidente VARCHAR(100)
        );
        
        ALTER TABLE accidentes ADD COLUMN IF NOT EXISTS anio INT;
        ALTER TABLE accidentes ADD COLUMN IF NOT EXISTS tipo_accidente VARCHAR(100);

        CREATE INDEX IF NOT EXISTS idx_accidentes_geom ON accidentes USING GIST(geom);
        CREATE INDEX IF NOT EXISTS idx_accidentes_anio ON accidentes(anio);
    """)
    await conn.close()


class RoutePayload(BaseModel):
    geojson_geometry: dict
    buffer_meters: float = 100.0


class RouteRequest(BaseModel):
    coordinates: List[List[float]]


@app.get("/")
async def serve_index():
    return FileResponse("static/index.html")


@app.get("/api/v1/accidentes/bbox")
async def get_accidentes_por_bbox(
    min_lon: float = Query(...),
    min_lat: float = Query(...),
    max_lon: float = Query(...),
    max_lat: float = Query(...)
):
    conn = await asyncpg.connect(DB_DSN)
    query = """
        SELECT id, codigo_carretera, pk, peso_severidad, ST_X(geom) AS lon, ST_Y(geom) AS lat
        FROM accidentes
        WHERE geom && ST_MakeEnvelope($1, $2, $3, $4, 4326)
        LIMIT 5000;
    """
    rows = await conn.fetch(query, min_lon, min_lat, max_lon, max_lat)
    await conn.close()

    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
            "properties": {
                "id": r["id"],
                "carretera": r["codigo_carretera"],
                "pk": float(r["pk"]) if r["pk"] else 0.0,
                "weight": r["peso_severidad"]
            }
        }
        for r in rows
    ]
    return {"type": "FeatureCollection", "features": features}


@app.post("/api/v1/siniestralidad-ruta")
async def calcular_siniestralidad_ruta(payload: RoutePayload):
    conn = await asyncpg.connect(DB_DSN)
    try:
        route_str = json.dumps(payload.geojson_geometry)
    except Exception:
        raise HTTPException(status_code=400, detail="GeoJSON inválido")

    query = """
        WITH ruta AS (
            SELECT ST_SetSRID(ST_GeomFromGeoJSON($1), 4326) AS geom
        )
        SELECT 
            COUNT(a.id) AS total_accidentes,
            COALESCE(SUM(a.fallecidos_30d), 0) AS total_fallecidos,
            COALESCE(SUM(a.heridos_graves), 0) AS total_heridos_graves,
            COALESCE(SUM(a.heridos_leves), 0) AS total_heridos_leves,
            COALESCE(SUM(a.peso_severidad), 0) AS indice_severidad_acumulado
        FROM accidentes a, ruta r
        WHERE ST_DWithin(a.geom::geography, r.geom::geography, $2);
    """
    res = await conn.fetchrow(query, route_str, payload.buffer_meters)
    await conn.close()

    score = res["indice_severidad_acumulado"]
    nivel = "Bajo" if score <= 10 else ("Medio" if score <= 30 else "Alto")

    return {
        "resumen_siniestralidad": {
            "total_accidentes": res["total_accidentes"],
            "fallecidos": res["total_fallecidos"],
            "heridos_graves": res["total_heridos_graves"],
            "heridos_leves": res["total_heridos_leves"],
            "puntuacion_severidad": score,
            "nivel_riesgo_estimado": nivel
        }
    }


@app.post("/api/v1/rutas/analizar")
async def analizar_riesgo_ruta(req: RouteRequest):
    if not req.coordinates or len(req.coordinates) < 2:
        return {"error": "Se requieren al menos 2 puntos para formar una ruta."}

    line_coords = ", ".join([f"{lon} {lat}" for lon, lat in req.coordinates])
    wkt_line = f"LINESTRING({line_coords})"

    conn = await asyncpg.connect(DB_DSN)
    
    # 1. Totales acumulados, número de años y longitud exacta del trazado en km vía PostGIS
    query_totales = """
        WITH ruta_geom AS (
            SELECT ST_GeomFromText($1, 4326) AS geom
        )
        SELECT 
            COUNT(a.id) AS total_accidentes,
            COALESCE(SUM(a.fallecidos_30d), 0) AS total_fallecidos,
            COALESCE(SUM(a.heridos_graves), 0) AS total_graves,
            COALESCE(SUM(a.heridos_leves), 0) AS total_leves,
            COALESCE(SUM(a.peso_severidad), 0) AS indice_riesgo_total,
            COUNT(DISTINCT a.anio) AS num_anios,
            -- Cálculo de la distancia real sobre el elipsoide terrestre en kilómetros
            ST_Length(rg.geom::geography) / 1000.0 AS longitud_km
        FROM ruta_geom rg
        LEFT JOIN accidentes a ON ST_DWithin(a.geom, rg.geom, 0.005)
        GROUP BY rg.geom;
    """
    row = await conn.fetchrow(query_totales, wkt_line)

    # 2. Causa principal predominante
    query_causa = """
        SELECT tipo_accidente, COUNT(*) as cantidad
        FROM accidentes
        WHERE ST_DWithin(
            geom, 
            ST_GeomFromText($1, 4326), 
            0.005
        ) 
          AND tipo_accidente IS NOT NULL 
          AND tipo_accidente NOT IN ('Desconocido', '0', '')
        GROUP BY tipo_accidente
        ORDER BY cantidad DESC
        LIMIT 1;
    """
    causa_row = await conn.fetchrow(query_causa, wkt_line)
    await conn.close()

    total_acc = row["total_accidentes"]
    fallecidos = row["total_fallecidos"]
    graves = row["total_graves"]
    leves = row["total_leves"]
    riesgo_total = float(row["indice_riesgo_total"])
    num_anios = max(row["num_anios"] or 1, 1)
    longitud_km = max(float(row["longitud_km"] or 0.1), 0.1)

    # Cálculo normalizado: Densidad de riesgo por año y por kilómetro de carretera
    riesgo_anual_km = (riesgo_total / num_anios) / longitud_km

    # Umbrales ajustados a la densidad por km (Riesgo Colectivo Relativo)
    nivel_riesgo = "Bajo"
    if riesgo_anual_km > 2.0:
        nivel_riesgo = "Alto"
    elif riesgo_anual_km > 0.5:
        nivel_riesgo = "Medio"

    # Traducir el código numérico o devolver el valor tal cual si ya venía formateado
    causa_raw = str(causa_row["tipo_accidente"]).strip() if causa_row else ""
    if causa_raw.endswith(".0"):
        causa_raw = causa_raw[:-2]
    elif "." in causa_raw:
        try:
            causa_raw = str(int(float(causa_raw)))
        except ValueError:
            pass

    causa_principal = TIPO_ACCIDENTE_MAP.get(causa_raw, causa_raw if causa_raw else "No especificada")

    return {
        "resumen": {
            "longitud_ruta_km": round(longitud_km, 2),
            "periodo_anos_analizados": num_anios,
            "total_accidentes_acumulados": total_acc,
            "fallecidos_acumulados": fallecidos,
            "heridos_graves_acumulados": graves,
            "heridos_leves_acumulados": leves,
            "causa_principal": causa_principal,
            "indice_riesgo_anual_km": round(riesgo_anual_km, 2),
            "nivel_riesgo": nivel_riesgo
        }
    }