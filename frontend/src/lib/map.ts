import maplibregl from "maplibre-gl";

/**
 * Basemap style.
 *
 * Raster OSM tiles rather than a vector style from a hosted provider: a vector style
 * needs an API key and an outbound call to a third party, and the platform must
 * render on an isolated government network where neither is available. Raster tiles
 * degrade to blank grey rather than failing the whole map.
 */
export const BASEMAP: maplibregl.StyleSpecification = {
  version: 8,
  sources: {
    osm: {
      type: "raster",
      tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
      tileSize: 256,
      attribution: "© OpenStreetMap contributors",
    },
  },
  layers: [
    { id: "bg", type: "background", paint: { "background-color": "#0b0f14" } },
    {
      id: "osm",
      type: "raster",
      source: "osm",
      paint: {
        // Desaturate and darken so operational overlays stay legible on top.
        "raster-opacity": 0.55,
        "raster-saturation": -0.7,
        "raster-brightness-max": 0.7,
      },
    },
  ],
};

// Gujarat, framed to include Kutch through to the southern districts.
export const GUJARAT_CENTER: [number, number] = [71.8, 22.6];
export const GUJARAT_ZOOM = 6.1;

export const STATUS_COLOUR: Record<string, string> = {
  ACTIVE: "#31c48d",
  DEGRADED: "#f0a13a",
  UNREACHABLE: "#f05252",
  DRAFT: "#8ba0bd",
  PROBING: "#4da3ff",
};

export function statusColour(status: string) {
  return STATUS_COLOUR[status] ?? "#8ba0bd";
}

/** Circle approximating a confidence radius, in GeoJSON. */
export function circlePolygon(lon: number, lat: number, radiusM: number, steps = 48) {
  const coords: [number, number][] = [];
  const latRad = (lat * Math.PI) / 180;
  const dLat = radiusM / 111_320;
  const dLon = radiusM / (111_320 * Math.cos(latRad));
  for (let i = 0; i <= steps; i++) {
    const theta = (i / steps) * 2 * Math.PI;
    coords.push([lon + dLon * Math.cos(theta), lat + dLat * Math.sin(theta)]);
  }
  return { type: "Polygon" as const, coordinates: [coords] };
}
