import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

/**
 * Standard shadcn helper: merge Tailwind class names with conflict
 * resolution. `cn("p-2", "p-4")` returns `"p-4"` rather than the literal
 * concatenation. Used in every component that takes a `className` prop.
 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
