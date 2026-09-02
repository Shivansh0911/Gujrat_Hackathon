import { useCallback, useEffect, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import {
  BASEMAP,
  BASEMAP_BACKGROUND,
  BASEMAP_PAINT,
  GUJARAT_CENTER,
  GUJARAT_ZOOM,
} from "./map";
import { useTheme } from "./theme";

/**
 * Create a MapLibre map bound to a container element.
 *
 * Two failure modes this exists to prevent, both of which produce a permanently
 * blank canvas with no error in the console:
 *
 * 1. **The container does not exist yet.** A page that early-returns a spinner while
 *    data loads never attaches its ref on first render. An effect keyed on a ref
 *    object bails once and never retries, because the ref identity never changes.
 *    A callback ref stored in state re-runs the effect the moment the node appears.
 *
 * 2. **The container has no size yet.** MapLibre measures once, at construction, and
 *    inside a flex layout the container is frequently 0x0 at that instant. A
 *    ResizeObserver calls resize() when the layout settles.
 */
export function useMapLibre() {
  const [container, setContainer] = useState<HTMLDivElement | null>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const { theme } = useTheme();

  const ref = useCallback((node: HTMLDivElement | null) => setContainer(node), []);

  useEffect(() => {
    if (!container || mapRef.current) return;

    const map = new maplibregl.Map({
      container,
      style: BASEMAP,
      center: GUJARAT_CENTER,
      zoom: GUJARAT_ZOOM,
      attributionControl: { compact: true },
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");
    mapRef.current = map;

    const observer = new ResizeObserver(() => map.resize());
    observer.observe(container);
    requestAnimationFrame(() => map.resize());

    return () => {
      observer.disconnect();
      map.remove();
      mapRef.current = null;
    };
  }, [container]);

  // Recolour the basemap in place when the theme changes. `setStyle` would take every
  // source and layer the pages have added with it, and the map would come back empty.
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const apply = () => {
      if (!map.getLayer("osm")) return;
      for (const [k, v] of Object.entries(BASEMAP_PAINT[theme])) {
        map.setPaintProperty("osm", k as never, v as never);
      }
      map.setPaintProperty("bg", "background-color", BASEMAP_BACKGROUND[theme]);
    };
    if (map.isStyleLoaded()) apply();
    else map.once("styledata", apply);
  }, [theme]);

  return { ref, mapRef, ready: container !== null };
}
