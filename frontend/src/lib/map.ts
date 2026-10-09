import maplibregl from "maplibre-gl";

/**
 * Basemap style.
 *
 * Raster OSM tiles rather than a vector style from a hosted provider: a vector style
 * needs an API key and an outbound call to a third party, and the platform must
 * render on an isolated government network where neither is available. Raster tiles
 * degrade to blank grey rather than failing the whole map.
 */
/**
 * How the OSM raster is treated in each theme.
 *
 * On the dark ground the tiles are desaturated and dimmed so the operational overlays
 * -- status dots, confidence circles, journey lines -- stay the brightest thing on
 * screen. On the light ground that treatment would produce grey mud, so the tiles are
 * shown nearly as published and only slightly desaturated, which is enough to keep the
 * overlay colours dominant without washing the map out.
 *
 * Applied with `setPaintProperty` on a theme change rather than by rebuilding the
 * style: `setStyle` would discard every source and layer the pages have added, and
 * the map would come back empty.
 */
export const BASEMAP_PAINT = {
  dark: {
    "raster-opacity": 0.55,
    "raster-saturation": -0.7,
    "raster-brightness-max": 0.7,
  },
  light: {
    "raster-opacity": 0.9,
    "raster-saturation": -0.25,
    "raster-brightness-max": 1,
  },
} as const;

export const BASEMAP_BACKGROUND = { dark: "#0b0f14", light: "#f8fafc" } as const;

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
      paint: BASEMAP_PAINT.dark,
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

/**
 * A viewshed wedge: what one camera can actually see.
 *
 * `heading_deg` is compass bearing -- 0 is north, increasing clockwise -- while the
 * trigonometry below works in the mathematical convention where 0 is east and angles
 * increase anticlockwise. The conversion is `90 - bearing`, and getting it wrong
 * produces cones that look plausible and point the wrong way, which is worse than
 * cones that obviously break.
 *
 * Longitude is divided by cos(latitude) because a degree of longitude is shorter the
 * further from the equator you are. At Gujarat's latitude that is a 7% error, which is
 * invisible on one cone and obvious when two adjacent cameras are compared.
 */
export function sectorPolygon(
  lon: number,
  lat: number,
  radiusM: number,
  headingDeg: number,
  fovDeg: number,
  steps = 24,
) {
  const latRad = (lat * Math.PI) / 180;
  const dLat = radiusM / 111_320;
  const dLon = radiusM / (111_320 * Math.cos(latRad));

  const centre = ((90 - headingDeg) * Math.PI) / 180;
  const half = ((fovDeg / 2) * Math.PI) / 180;

  // Apex first, then the arc, then back to the apex. The apex is the camera, which is
  // what makes the shape read as a viewshed rather than as a pie slice floating on
  // the map.
  const coords: [number, number][] = [[lon, lat]];
  for (let i = 0; i <= steps; i++) {
    const theta = centre - half + (i / steps) * 2 * half;
    coords.push([lon + dLon * Math.cos(theta), lat + dLat * Math.sin(theta)]);
  }
  coords.push([lon, lat]);
  return { type: "Polygon" as const, coordinates: [coords] };
}

/**
 * Default field of view, in degrees, when a camera does not publish one.
 *
 * 55 degrees is a typical fixed-lens CCTV horizontal FOV. It is an assumption and is
 * labelled as one in the legend: drawing a precise-looking cone from a number nobody
 * measured is the kind of thing this project refuses elsewhere, and a reviewer who
 * discovers it unlabelled stops trusting the cones that *are* derived.
 */
export const ASSUMED_FOV_DEG = 55;

/**
 * Effective range in metres for drawing, when nothing better is known.
 *
 * Not the same question as `confidence_radius_m`, which is how well we know where the
 * camera IS. This is how far it can usefully see, and the two are independent: a
 * precisely located camera can have a short range, and vice versa.
 */
export const ASSUMED_RANGE_M = 120;
