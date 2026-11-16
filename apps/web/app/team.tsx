"use client";

import { useCallback, useEffect, useState } from "react";
import { api, ApiError, day, errorText, post, type Workspace } from "./api";

type Usage = { plan: string; used: Record<Limit, number>; limits: Record<Limit, number> };
type Limit = "questions" | "documents" | "people";
const LIMIT_LABELS: [Limit, string][] = [
  ["questions", "Questions this month"],
  ["documents", "Documents"],
  ["people", "People"],
];

type Member = { id: number; email: string; role: "admin" | "member"; workspace_ids: number[]; you: boolean };
type Invite = { id: number; role: "admin" | "member"; expires_at: string };

export default function Team({ workspaces }: { workspaces: Workspace[] }) {
  const [members, setMembers] = useState<Member[]>([]);
  const [error, setError] = useState("");
  const [role, setRole] = useState("member");
  const [link, setLink] = useState("");
  const [copied, setCopied] = useState(false);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [rowError, setRowError] = useState<{ id: number; text: string } | null>(null);
  const [invites, setInvites] = useState<Invite[] | null>(null);
  const [inviteError, setInviteError] = useState("");

  const load = useCallback(
    () => api<Member[]>("/team").then(setMembers).catch((e) => setError(e.message)),
    [],
  );
  const loadInvites = useCallback(
    () =>
      api<Invite[]>("/invites")
        .then((list) => {
          setInvites(list);
          setInviteError("");
        })
        .catch((e) => setInviteError(errorText(e, "Could not load the pending invites"))),
    [],
  );
  useEffect(() => {
    load();
    loadInvites();
    api<Usage>("/usage").then(setUsage).catch(() => {});
  }, [load, loadInvites]);

  async function changeRole(member: Member, next: "admin" | "member") {
    if (next === "admin" && !window.confirm(`Make ${member.email} an admin? Admins open every workspace and manage the team.`)) return;
    setRowError(null);
    try {
      await api(`/team/${member.id}/role`, { method: "PUT", body: JSON.stringify({ role: next }) });
    } catch (e) {
      setRowError({ id: member.id, text: errorText(e, "Could not change the role. Try again.") });
    }
    load();
  }

  async function revoke(invite: Invite) {
    setInviteError("");
    try {
      await api(`/invites/${invite.id}`, { method: "DELETE" });
    } catch (e) {
      // 404: already used, expired or revoked; the refreshed list shows that.
      if (!(e instanceof ApiError && e.status === 404)) setInviteError(errorText(e, "Could not revoke the invite. Try again."));
    }
    loadInvites();
  }

  async function toggle(member: Member, workspace: Workspace, allowed: boolean) {
    try {
      await api(`/workspaces/${workspace.id}/members/${member.id}`, { method: allowed ? "PUT" : "DELETE" });
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not change access");
    }
    load();
  }

  async function remove(member: Member) {
    if (!window.confirm(`Remove ${member.email} from the company? They are signed out at once and lose all access.`)) return;
    try {
      await api(`/team/${member.id}`, { method: "DELETE" });
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not remove this person");
    }
    load();
  }

  async function createInvite(e: React.FormEvent) {
    e.preventDefault();
    try {
      const r = await api<{ token: string }>("/invites", post({ role }));
      setLink(`${window.location.origin}/?invite=${r.token}`);
      setCopied(false);
      setError("");
      loadInvites();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not create the invite link");
    }
  }

  return (
    <div className="team">
      <h1>Team and access</h1>
      {error && <p className="error" role="alert">{error}</p>}

      {usage && (
        <section>
          <h2>Plan: {usage.plan}</h2>
          <ul className="usage">
            {LIMIT_LABELS.map(([key, label]) => {
              const share = Math.min(1, usage.used[key] / usage.limits[key]);
              return (
                <li key={key}>
                  <span>{label}</span>
                  <meter min={0} max={usage.limits[key]} value={usage.used[key]} high={usage.limits[key] * 0.8} optimum={0} aria-label={label} />
                  <span className={share >= 1 ? "error" : undefined}>
                    {usage.used[key]} of {usage.limits[key]}
                  </span>
                </li>
              );
            })}
          </ul>
        </section>
      )}

      <section>
        <h2>Invite someone</h2>
        <form className="row" onSubmit={createInvite}>
          <label>
            They join as{" "}
            <select value={role} onChange={(e) => setRole(e.target.value)}>
              <option value="member">a member, who sees only the workspaces you tick</option>
              <option value="admin">an admin, who sees every workspace</option>
            </select>
          </label>
          <button>Create invite link</button>
        </form>
        {link && (
          <div className="invite">
            <input readOnly value={link} aria-label="Invite link" onFocus={(e) => e.target.select()} />
            <button
              onClick={async () => {
                await navigator.clipboard.writeText(link);
                setCopied(true);
              }}
            >
              {copied ? "Copied" : "Copy link"}
            </button>
            <p>Send this link yourself. It works once and stops working after 7 days.</p>
          </div>
        )}
      </section>

      <section>
        <h2>Pending invites</h2>
        {inviteError && <p className="error" role="alert">{inviteError}</p>}
        {invites?.length === 0 && <p className="quiet">No unused invite links.</p>}
        {invites && invites.length > 0 && (
          <ul className="invites">
            {invites.map((inv) => (
              <li key={inv.id}>
                <span>Joins as {inv.role === "admin" ? "an admin" : "a member"}</span>
                <span className="quiet">stops working on {day(inv.expires_at)}</span>
                <button className="link" aria-label={`Revoke the invite that joins as ${inv.role}, ending ${day(inv.expires_at)}`} onClick={() => revoke(inv)}>
                  Revoke
                </button>
              </li>
            ))}
          </ul>
        )}
        {invites && invites.length > 0 && <p className="quiet">Revoke a link you sent to the wrong person. It stops working at once.</p>}
      </section>

      <section>
        <h2>Who can open which client</h2>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Person</th>
                {workspaces.map((w) => (
                  <th scope="col" key={w.id}>{w.name}</th>
                ))}
                <th scope="col"><span className="visually-hidden">Remove</span></th>
              </tr>
            </thead>
            <tbody>
              {members.map((m) => (
                <tr key={m.id}>
                  <th scope="row">
                    {m.email}
                    {m.you ? (
                      m.role === "admin" && <span className="role">admin</span>
                    ) : (
                      <select
                        className="role-select"
                        aria-label={`Role of ${m.email}`}
                        value={m.role}
                        onChange={(e) => changeRole(m, e.target.value as "admin" | "member")}
                      >
                        <option value="member">member</option>
                        <option value="admin">admin</option>
                      </select>
                    )}
                    {rowError?.id === m.id && <p className="error" role="alert">{rowError.text}</p>}
                  </th>
                  {workspaces.map((w) => (
                    <td key={w.id}>
                      <input
                        type="checkbox"
                        aria-label={`${m.email} can open ${w.name}`}
                        checked={m.role === "admin" || m.workspace_ids.includes(w.id)}
                        disabled={m.role === "admin"}
                        onChange={(e) => toggle(m, w, e.target.checked)}
                      />
                    </td>
                  ))}
                  <td>
                    {m.you ? (
                      <span className="role">you</span>
                    ) : (
                      <button className="link" aria-label={`Remove ${m.email}`} onClick={() => remove(m)}>
                        Remove
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {workspaces.length === 0 && <p>Create a workspace first, then tick who can open it.</p>}
      </section>
    </div>
  );
}
