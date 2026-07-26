"use client";

import { useEffect, useMemo, useState } from "react";

import { cn } from "@/lib/utils";

type ImageThumbnailProps = {
  src: string;
  thumbnailSrc?: string;
  alt?: string;
  className?: string;
  imageClassName?: string;
};

export function getImageThumbnailUrl(src: string) {
  const marker = "/images/";
  const index = src.indexOf(marker);
  if (index < 0) return src;
  const rel = src.slice(index + marker.length);
  return `${src.slice(0, index)}/image-thumbnails/${rel}.webp`;
}

export function getLegacyPngThumbnailUrl(src: string) {
  const marker = "/images/";
  const index = src.indexOf(marker);
  if (index < 0) return src;
  return `${src.slice(0, index)}/image-thumbnails/${src.slice(index + marker.length)}`;
}

export function ImageThumbnail({ src, thumbnailSrc, alt = "", className, imageClassName }: ImageThumbnailProps) {
  const webpSrc = useMemo(() => thumbnailSrc || getImageThumbnailUrl(src), [src, thumbnailSrc]);
  const pngSrc = useMemo(() => getLegacyPngThumbnailUrl(src), [src]);
  const [currentSrc, setCurrentSrc] = useState(webpSrc);
  const [stage, setStage] = useState<"webp" | "png" | "full">("webp");

  useEffect(() => {
    setCurrentSrc(webpSrc);
    setStage("webp");
  }, [webpSrc]);

  return (
    <span className={cn("block overflow-hidden bg-stone-100", className)}>
      <img
        src={currentSrc}
        alt={alt}
        className={cn("h-full w-full object-cover", imageClassName)}
        loading="lazy"
        decoding="async"
        fetchPriority="low"
        onError={() => {
          if (stage === "webp") {
            setStage("png");
            setCurrentSrc(pngSrc);
            return;
          }
          if (stage === "png" && currentSrc !== src) {
            setStage("full");
            setCurrentSrc(src);
          }
        }}
      />
    </span>
  );
}
