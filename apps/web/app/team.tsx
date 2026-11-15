"use client";

import { useCallback, useEffect, useState } from "react";
import { api, post, type Workspace } from "./api";

type Member = { id: number; email: string; role: "admin" | "member"; workspace_ids: number[] };

export default function Team({ workspaces }: { workspaces: Workspace[] }) {
  const [members, setMembers] = useState<Member[]>([]);
  const [error, setError] = useState("");
  const [role, setRole] = useState("member");
  const [link, setLink] = useState("");
  const [copied, setCopied] = useState(false);

  const load = useCallback(
    () => api<Member[]>("/team").then(setMembers).catch((e) => setError(e.message)),
    [],
  );
  useEffect(() => {
    load();
  }, [load]);

  async function toggle(member: Member, workspace: Workspace, allowed: boolean) {
    try {
      await api(`/workspaces/${workspace.id}/members/${member.id}`, { method: allowed ? "PUT" : "DELETE" });
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not change access");
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
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not create the invite link");
    }
  }

  return (
    <div className="team">
      <h1>Team and access</h1>
      {error && <p className="error" role="alert">{error}</p>}

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
        <h2>Who can open which client</h2>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Person</th>
                {workspaces.map((w) => (
                  <th scope="col" key={w.id}>{w.name}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {members.map((m) => (
                <tr key={m.id}>
                  <th scope="row">
                    {m.email}
                    {m.role === "admin" && <span className="role">admin</span>}
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
