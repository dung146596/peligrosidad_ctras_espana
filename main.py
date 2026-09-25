import os
import json
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import asyncpg
from pydantic import BaseModel
from typing import List
from dotenv import load_dotenv

load_dotenv()

DB_DSN = os.getenv("DB_DSN", "postgresql://postgres:postgrespassword@localhost:5432/dgt_risk_db")

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
    """Crea la tabla de accidentes si no existe e inserta datos de prueba si está vacía."""
    conn = await asyncpg.connect(DB_DSN)
    
    # Crear extensión PostGIS y tabla
    await conn.execute("""
        CREATE EXTENSION IF NOT EXISTS postgis;
        
        CREATE TABLE IF NOT EXISTS accidentes (
            id SERIAL PRIMARY KEY,
            codigo_carretera VARCHAR(20),
            pk NUMERIC(6,2),
            fallecidos_30d INT DEFAULT 0,
            heridos_graves INT DEFAULT 0,
            heridos_leves INT DEFAULT 0,
            peso_severidad FLOAT,
            geom GEOMETRY(Point, 4326)
        );
        
        CREATE INDEX IF NOT EXISTS idx_accidentes_geom ON accidentes USING GIST(geom);
    """)
    
    # Insertar datos simulados de prueba si la tabla está vacía (puntos alrededor de Madrid)
    count = await conn.fetchval("SELECT COUNT(*) FROM accidentes")
    if count == 0:
        await conn.execute("""
            INSERT INTO accidentes (codigo_carretera, pk, fallecidos_30d, heridos_graves, heridos_leves, peso_severidad, geom)
            VALUES 
            ('A-6', 15.2, 1, 2, 0, 16.0, ST_SetSRID(ST_MakePoint(-3.7654, 40.4532), 4326)),
            ('A-6', 16.0, 0, 1, 3, 6.0, ST_SetSRID(ST_MakePoint(-3.7710, 40.4580), 4326)),
            ('A-1', 20.5, 2, 0, 1, 21.0, ST_SetSRID(ST_MakePoint(-3.6210, 40.5410), 4326)),
            ('M-30', 5.0, 0, 0, 2, 2.0, ST_SetSRID(ST_MakePoint(-3.6700, 40.4300), 4326));
        """)
    await conn.close()

class RoutePayload(BaseModel):
    geojson_geometry: dict
    buffer_meters: float = 100.0

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
                "pk": float(r["pk"]),
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

class RouteRequest(BaseModel):
    # Lista de coordenadas [longitude, latitude] de la ruta
    coordinates: List[List[float]]

@app.post("/api/v1/rutas/analizar")
async def analizar_riesgo_ruta(req: RouteRequest):
    if not req.coordinates or len(req.coordinates) < 2:
        return {"error": "Se requieren al menos 2 puntos para formar una ruta."}

    line_coords = ", ".join([f"{lon} {lat}" for lon, lat in req.coordinates])
    wkt_line = f"LINESTRING({line_coords})"

    conn = await asyncpg.connect(DB_DSN)
    
    # Consultar totales acumulados y contar el número de años analizados
    query = """
        SELECT 
            COUNT(*) AS total_accidentes,
            COALESCE(SUM(fallecidos_30d), 0) AS total_fallecidos,
            COALESCE(SUM(heridos_graves), 0) AS total_graves,
            COALESCE(SUM(heridos_leves), 0) AS total_leves,
            COALESCE(SUM(peso_severidad), 0) AS indice_riesgo_total,
            COUNT(DISTINCT anio) AS num_anios
        FROM accidentes
        WHERE ST_DWithin(
            geom, 
            ST_GeomFromText($1, 4326), 
            0.005
        );
    """
    
    row = await conn.fetchrow(query, wkt_line)
    await conn.close()

    total_acc = row["total_accidentes"]
    fallecidos = row["total_fallecidos"]
    graves = row["total_graves"]
    leves = row["total_leves"]
    riesgo_total = float(row["indice_riesgo_total"])
    num_anios = max(row["num_anios"] or 1, 1)

    # Riesgo promedio anual en el tramo
    riesgo_anual = riesgo_total / num_anios

    # Clasificación basada en el promedio anual de la ruta
    nivel_riesgo = "Bajo"
    if riesgo_anual > 30:
        nivel_riesgo = "Alto"
    elif riesgo_anual > 10:
        nivel_riesgo = "Medio"

    return {
        "resumen": {
            "periodo_anos_analizados": num_anios,
            "total_accidentes_acumulados": total_acc,
            "fallecidos_acumulados": fallecidos,
            "heridos_graves_acumulados": graves,
            "heridos_leves_acumulados": leves,
            "indice_riesgo_anual_promedio": round(riesgo_anual, 2),
            "nivel_riesgo": nivel_riesgo
        }
    }