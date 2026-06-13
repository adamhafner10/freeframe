"use client";

import * as React from "react";
import useSWR from "swr";
import { HardDrive, Database, Folder } from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { EmptyState } from "@/components/shared/empty-state";
import { Skeleton } from "@/components/shared/skeleton";
import { useAuthStore } from "@/stores/auth-store";
import { useRouter } from "next/navigation";

interface ProjectStorage {
  project_id: string;
  name: string;
  bytes: number;
  bytes_human: string;
  asset_count: number;
  version_count: number;
}

interface StorageSummary {
  total_bytes: number;
  total_human: string;
  project_count: number;
  projects: ProjectStorage[];
}

function formatCount(n: number, singular: string): string {
  return `${n.toLocaleString()} ${n === 1 ? singular : singular + "s"}`;
}

export default function StoragePage() {
  const { user, isSuperAdmin } = useAuthStore();
  const router = useRouter();

  const { data, error, isLoading } = useSWR<StorageSummary>(
    isSuperAdmin ? "/admin/storage" : null,
    () => api.get<StorageSummary>("/admin/storage"),
  );

  React.useEffect(() => {
    if (user && !isSuperAdmin) {
      router.replace("/");
    }
  }, [user, isSuperAdmin, router]);

  if (!isSuperAdmin) {
    return null;
  }

  // Sort projects by bytes desc; compute proportional bars off the largest project.
  const projects = React.useMemo(() => {
    if (!data?.projects) return [];
    return [...data.projects].sort((a, b) => b.bytes - a.bytes);
  }, [data]);

  const maxBytes = projects.length > 0 ? Math.max(projects[0].bytes, 1) : 1;

  return (
    <div className="p-6 space-y-8">
      {/* Header */}
      <div className="flex items-center gap-3">
        <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-accent-muted">
          <HardDrive className="h-5 w-5 text-accent" />
        </div>
        <div>
          <h1 className="text-xl font-semibold text-text-primary">
            Storage &amp; Usage
          </h1>
          <p className="text-sm text-text-secondary">
            Per-project storage breakdown across the platform
          </p>
        </div>
      </div>

      {/* Error banner */}
      {error && (
        <div
          role="alert"
          className="flex items-start justify-between gap-3 rounded-lg border border-status-error/30 bg-status-error/10 px-4 py-3"
        >
          <p className="text-sm text-status-error">
            {error instanceof Error
              ? error.message
              : "Failed to load storage usage."}
          </p>
        </div>
      )}

      {/* Summary cards */}
      <section className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className="rounded-lg border border-border bg-bg-secondary p-4">
          <div className="flex items-center gap-2 text-text-tertiary">
            <Database className="h-4 w-4" />
            <span className="text-xs font-medium uppercase tracking-wider">
              Total storage
            </span>
          </div>
          {isLoading ? (
            <Skeleton className="mt-2 h-8 w-28" />
          ) : (
            <p className="mt-2 text-2xl font-semibold text-text-primary tabular-nums">
              {data?.total_human ?? "0 B"}
            </p>
          )}
        </div>

        <div className="rounded-lg border border-border bg-bg-secondary p-4">
          <div className="flex items-center gap-2 text-text-tertiary">
            <Folder className="h-4 w-4" />
            <span className="text-xs font-medium uppercase tracking-wider">
              Projects
            </span>
          </div>
          {isLoading ? (
            <Skeleton className="mt-2 h-8 w-16" />
          ) : (
            <p className="mt-2 text-2xl font-semibold text-text-primary tabular-nums">
              {(data?.project_count ?? 0).toLocaleString()}
            </p>
          )}
        </div>
      </section>

      {/* Per-project breakdown */}
      <section className="space-y-4">
        <h2 className="text-sm font-semibold text-text-primary">
          Storage by project
        </h2>

        {isLoading ? (
          <div className="space-y-2">
            {Array.from({ length: 5 }).map((_, i) => (
              <Skeleton key={i} className="h-16 w-full rounded-lg" />
            ))}
          </div>
        ) : projects.length === 0 ? (
          <div className="rounded-lg border border-border bg-bg-secondary">
            <EmptyState
              icon={HardDrive}
              title="No storage usage"
              description="Storage will appear here once projects contain assets."
            />
          </div>
        ) : (
          <div className="rounded-lg border border-border bg-bg-secondary overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border bg-bg-tertiary">
                  <th className="px-4 py-2.5 text-left text-xs font-medium text-text-tertiary">
                    Project
                  </th>
                  <th className="hidden px-4 py-2.5 text-left text-xs font-medium text-text-tertiary sm:table-cell">
                    Share
                  </th>
                  <th className="px-4 py-2.5 text-right text-xs font-medium text-text-tertiary">
                    Assets
                  </th>
                  <th className="px-4 py-2.5 text-right text-xs font-medium text-text-tertiary">
                    Versions
                  </th>
                  <th className="px-4 py-2.5 text-right text-xs font-medium text-text-tertiary">
                    Size
                  </th>
                </tr>
              </thead>
              <tbody>
                {projects.map((p) => {
                  const pct = Math.round((p.bytes / maxBytes) * 100);
                  return (
                    <tr
                      key={p.project_id}
                      className="border-b border-border last:border-0 hover:bg-bg-tertiary transition-colors"
                    >
                      <td className="px-4 py-3">
                        <p className="text-sm font-medium text-text-primary truncate max-w-[200px]">
                          {p.name}
                        </p>
                        {/* Bar shown inline on small screens where the Share column is hidden */}
                        <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-bg-tertiary sm:hidden">
                          <div
                            className="h-full rounded-full bg-accent"
                            style={{ width: `${Math.max(pct, 2)}%` }}
                          />
                        </div>
                      </td>
                      <td className="hidden px-4 py-3 align-middle sm:table-cell">
                        <div className="flex items-center gap-2">
                          <div className="h-1.5 w-32 overflow-hidden rounded-full bg-bg-tertiary">
                            <div
                              className="h-full rounded-full bg-accent"
                              style={{ width: `${Math.max(pct, 2)}%` }}
                            />
                          </div>
                          <span className="text-xs text-text-tertiary tabular-nums w-9 text-right">
                            {pct}%
                          </span>
                        </div>
                      </td>
                      <td className="px-4 py-3 text-right text-xs text-text-secondary tabular-nums">
                        {p.asset_count.toLocaleString()}
                      </td>
                      <td className="px-4 py-3 text-right text-xs text-text-secondary tabular-nums">
                        {p.version_count.toLocaleString()}
                      </td>
                      <td className="px-4 py-3 text-right text-sm font-medium text-text-primary tabular-nums whitespace-nowrap">
                        {p.bytes_human}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {!isLoading && projects.length > 0 && (
          <p className="text-xs text-text-tertiary">
            {formatCount(projects.length, "project")} ·{" "}
            {data?.total_human ?? "0 B"} total. Share is relative to the largest
            project.
          </p>
        )}
      </section>
    </div>
  );
}
