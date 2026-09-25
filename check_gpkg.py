import geopandas as gpd

path_gpkg = "geopackage/rt_viaria.gpkg"

print("🔍 1. Columnas en la capa de PKs (rt_ppkk_p):")
gdf_pk = gpd.read_file(path_gpkg, layer="rt_ppkk_p", max_features=3, engine="pyogrio")
print(gdf_pk.columns.tolist())
print(gdf_pk.head(2))

print("\n🔍 2. Columnas en la capa de tramos viales (rt_tramo_vial):")
gdf_tramo = gpd.read_file(path_gpkg, layer="rt_tramo_vial", max_features=3, engine="pyogrio")
print(gdf_tramo.columns.tolist())
print(gdf_tramo.head(2))