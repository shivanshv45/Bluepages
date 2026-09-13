/* Film grain, shared.
 *
 * Canvas rather than an SVG filter or a tiled image: it is one small loop, it
 * costs nothing, and static grain reads as dirt on the screen rather than as
 * stock. Redrawn at 12fps, which is where grain stops looking like noise.
 *
 * Extracted out of Home.tsx so the login screen (and anywhere else that
 * wants the same cinematic ground) can use the identical texture rather than
 * a second implementation drifting out of sync with it.
 */

import { useEffect, useMemo, useRef, useState } from "react";

export function FilmGrain({ className = "grain" }: { className?: string }) {
  const ref = useRef<HTMLCanvasElement>(null);
  const reduced = usePrefersReducedMotion();

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const context = canvas.getContext("2d");
    if (!context) return;

    const W = 160;
    const H = 160;
    canvas.width = W;
    canvas.height = H;

    let raf = 0;
    let last = 0;

    const draw = (now: number) => {
      if (now - last > 83) {
        const image = context.createImageData(W, H);
        const pixels = image.data;
        for (let i = 0; i < pixels.length; i += 4) {
          const value = Math.random() * 255;
          pixels[i] = value;
          pixels[i + 1] = value;
          pixels[i + 2] = value;
          pixels[i + 3] = 10;
        }
        context.putImageData(image, 0, 0);
        last = now;
      }
      raf = requestAnimationFrame(draw);
    };

    if (!reduced) raf = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(raf);
  }, [reduced]);

  return <canvas ref={ref} className={className} aria-hidden="true" />;
}

export function usePrefersReducedMotion(): boolean {
  const query = useMemo(
    () =>
      typeof window !== "undefined" && window.matchMedia
        ? window.matchMedia("(prefers-reduced-motion: reduce)")
        : null,
    [],
  );
  const [reduced, setReduced] = useState(query?.matches ?? false);

  useEffect(() => {
    if (!query) return;
    const handle = () => setReduced(query.matches);
    query.addEventListener("change", handle);
    return () => query.removeEventListener("change", handle);
  }, [query]);

  return reduced;
}
