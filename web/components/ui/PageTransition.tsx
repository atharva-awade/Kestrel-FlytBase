"use client";

import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { usePathname } from "next/navigation";

/**
 * Route changes that resolve rather than cut.
 *
 * Navigating between decks used to swap one full screen for another in a single
 * frame, which reads as a page reload even though nothing reloaded. A short lift
 * and fade tells the eye that one view replaced another, and it costs nothing:
 * the transform runs on the compositor, and the incoming deck is already
 * rendered when it starts.
 *
 * Deliberately quick. A transition long enough to notice consciously is a
 * transition that gets in the way by the fifth navigation.
 */
export function PageTransition({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const reduced = useReducedMotion();

  if (reduced) return <>{children}</>;

  return (
    <AnimatePresence mode="wait" initial={false}>
      <motion.div
        key={pathname}
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        exit={{ opacity: 0, y: -6 }}
        transition={{ duration: 0.26, ease: [0.22, 1, 0.36, 1] }}
      >
        {children}
      </motion.div>
    </AnimatePresence>
  );
}
