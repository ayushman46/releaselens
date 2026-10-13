"use client";

import { Building, ChevronsUpDown, LogOut, Plus, Settings, UserRound } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { CreateOrgForm } from "@/components/session/create-org-form";
import { useSession } from "@/components/session/session-provider";
import { Dialog } from "@/components/ui/dialog";
import { Menu, MenuItem, MenuLabel, MenuSeparator } from "@/components/ui/menu";
import { Skeleton } from "@/components/ui/skeleton";

function roleLabel(role: string): string {
  return role.charAt(0).toUpperCase() + role.slice(1);
}

/** Top-bar organization switcher: the user's organizations, plus "Create organization". */
export function OrgSwitcher() {
  const session = useSession();
  const [creating, setCreating] = useState(false);

  if (session.status !== "signed-in") {
    return (
      <div className="flex h-9 items-center gap-2 px-1.5" aria-hidden="true">
        <Skeleton className="size-6" />
        <Skeleton className="h-4 w-28" />
      </div>
    );
  }

  const active = session.activeOrg;
  return (
    <>
      <Menu
        label="Organizations"
        trigger={(props) => (
          <button
            type="button"
            {...props}
            aria-label={active ? `Organization: ${active.name}. Switch organization` : "Choose an organization"}
            className="inline-flex h-9 min-w-0 shrink items-center gap-2 rounded-md px-1.5 text-left hover:bg-subtle"
          >
            <span className="flex size-6 shrink-0 items-center justify-center rounded-md border bg-subtle">
              <Building aria-hidden="true" className="size-3.5 text-muted-foreground" />
            </span>
            <span className="flex min-w-0 flex-col leading-tight">
              <span className="truncate text-[13px] font-medium text-foreground">{active?.name ?? "No organization"}</span>
              <span className="text-[11px] whitespace-nowrap text-muted-foreground">
                {active ? `${roleLabel(active.role)} · ${roleLabel(active.plan)} plan` : "Create one to start"}
              </span>
            </span>
            <ChevronsUpDown aria-hidden="true" className="size-3.5 shrink-0 text-muted-foreground" />
          </button>
        )}
      >
        <MenuLabel>Your organizations</MenuLabel>
        {session.memberships.map((m) => (
          <MenuItem
            key={m.org_id}
            checked={m.org_id === active?.org_id}
            description={`${roleLabel(m.role)} · ${m.slug}`}
            onSelect={() => session.switchOrg(m.org_id)}
          >
            {m.name}
          </MenuItem>
        ))}
        <MenuSeparator />
        <MenuItem onSelect={() => setCreating(true)}>
          <span className="inline-flex items-center gap-2">
            <Plus aria-hidden="true" className="size-4" />
            Create organization
          </span>
        </MenuItem>
      </Menu>
      <Dialog open={creating} onClose={() => setCreating(false)} label="Create organization" className="p-6">
        <h2 className="mb-4 text-base font-semibold">Create organization</h2>
        <CreateOrgForm onCreated={() => setCreating(false)} />
      </Dialog>
    </>
  );
}

export function UserMenu() {
  const session = useSession();
  const router = useRouter();
  if (session.status !== "signed-in" || !session.me) return null;
  const user = session.me.user;
  return (
    <Menu
      label="Account"
      align="end"
      trigger={(props) => (
        <button
          type="button"
          {...props}
          aria-label={`Account: ${user.email}`}
          className="inline-flex size-8 items-center justify-center rounded-full border bg-subtle text-xs font-semibold text-foreground-secondary hover:text-foreground"
        >
          {(user.display_name || user.email).charAt(0).toUpperCase()}
        </button>
      )}
    >
      <div className="px-2 py-1.5">
        <p className="truncate text-sm font-medium text-foreground">{user.display_name || user.email}</p>
        {user.display_name ? <p className="truncate text-xs text-muted-foreground">{user.email}</p> : null}
      </div>
      <MenuSeparator />
      <MenuItem onSelect={() => router.push("/app/settings")}>
        <span className="inline-flex items-center gap-2">
          <Settings aria-hidden="true" className="size-4" />
          Organization settings
        </span>
      </MenuItem>
      <MenuItem onSelect={() => router.push("/app/settings/members")}>
        <span className="inline-flex items-center gap-2">
          <UserRound aria-hidden="true" className="size-4" />
          Members
        </span>
      </MenuItem>
      <MenuSeparator />
      <MenuItem onSelect={() => void session.signOut()}>
        <span className="inline-flex items-center gap-2">
          <LogOut aria-hidden="true" className="size-4" />
          Sign out
        </span>
      </MenuItem>
    </Menu>
  );
}
