import { Skeleton } from "@/components/ui/primitives";

/** Shown while a route's data resolves, so navigation never lands on a blank page. */
export default function Loading() {
  return (
    <div className="mx-auto max-w-[1500px] px-5 py-6">
      <Skeleton className="mb-4 h-9 w-64" />
      <div className="mb-4 flex flex-wrap gap-2">
        {[0, 1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-[58px] w-[190px] flex-1" />
        ))}
      </div>
      <div className="grid gap-4 xl:grid-cols-[1fr_390px]">
        <Skeleton className="aspect-video w-full" />
        <div className="space-y-3">
          <Skeleton className="h-52" />
          <Skeleton className="h-40" />
        </div>
      </div>
    </div>
  );
}
