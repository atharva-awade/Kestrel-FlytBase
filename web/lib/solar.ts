/**
 * Is it dark at this point on the Earth right now?
 *
 * The command deck cares because every after-hours rule does: a site in daylight
 * and a site at 02:00 warrant different attention, and the fleet spans ten
 * countries, so "night" is a per-site fact rather than a property of the viewer.
 *
 * This is deliberately plain arithmetic rather than a 3D terminator overlay. The
 * useful part of a terminator is *which sites are dark*, and a number derived
 * from a formula can be unit-tested, whereas a rotated hemisphere can be
 * silently upside down and nobody notices until an interviewer does.
 *
 * Accuracy is roughly a degree, from the standard low-precision solar position
 * model (NOAA's simplified equations). More than enough to decide whether the
 * sun is up.
 */

const RAD = Math.PI / 180;

/** Solar declination in degrees for a given instant. */
export function solarDeclination(date: Date): number {
  const start = Date.UTC(date.getUTCFullYear(), 0, 0);
  const dayOfYear = (date.getTime() - start) / 86_400_000;
  // Axial tilt projected onto the orbit, with the perihelion correction.
  return (
    -23.44 *
    Math.cos(RAD * (360 / 365.24) * (dayOfYear + 10 +
      2 * Math.sin(RAD * (360 / 365.24) * (dayOfYear - 2))))
  );
}

/** Longitude where the sun is directly overhead. */
export function subsolarLongitude(date: Date): number {
  const utcHours =
    date.getUTCHours() + date.getUTCMinutes() / 60 + date.getUTCSeconds() / 3600;
  let lon = -15 * (utcHours - 12);
  while (lon > 180) lon -= 360;
  while (lon < -180) lon += 360;
  return lon;
}

/** Solar elevation in degrees at a point. Negative means the sun is below the horizon. */
export function solarElevation(lat: number, lon: number, date = new Date()): number {
  const decl = solarDeclination(date) * RAD;
  const hourAngle = (lon - subsolarLongitude(date)) * RAD;
  const phi = lat * RAD;
  const sinElev =
    Math.sin(phi) * Math.sin(decl) + Math.cos(phi) * Math.cos(decl) * Math.cos(hourAngle);
  return Math.asin(Math.max(-1, Math.min(1, sinElev))) / RAD;
}

/** True when the sun is below the horizon, using civil twilight (-6°) as the edge. */
export function isNight(lat: number, lon: number, date = new Date()): boolean {
  return solarElevation(lat, lon, date) < -6;
}

/** Local solar time as HH:MM, for showing what o'clock it is at a site. */
export function localSolarTime(lon: number, date = new Date()): string {
  const utcMinutes = date.getUTCHours() * 60 + date.getUTCMinutes();
  const offset = (lon / 15) * 60;
  let mins = (utcMinutes + offset) % 1440;
  if (mins < 0) mins += 1440;
  const h = Math.floor(mins / 60);
  const m = Math.floor(mins % 60);
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}
