"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import { Badge, Button, Card, ErrorBanner, Field, inputCls, PageHeader, Table, Td } from "@/components/ui";
import { api, ApiError } from "@/lib/client";
import { useApi } from "@/lib/hooks";
import type { Doc, KnowledgeBase, SearchHit } from "@/lib/types";

export default function Knowledge() {
  const { org } = useParams<{ org: string }>();
  const kbs = useApi<KnowledgeBase[]>(`/orgs/${org}/knowledge-bases`);
  const [kbId, setKbId] = useState<string>();
  const docs = useApi<Doc[]>(kbId ? `/orgs/${org}/knowledge-bases/${kbId}/documents` : null, 3000);
  const [newKb, setNewKb] = useState("");
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [query, setQuery] = useState("");
  const [strategy, setStrategy] = useState("hybrid");
  const [hits, setHits] = useState<SearchHit[]>();
  const [error, setError] = useState<ApiError>();

  useEffect(() => {
    if (!kbId && kbs.data?.[0]) setKbId(kbs.data[0].id);
  }, [kbs.data, kbId]);

  async function guard(fn: () => Promise<void>) {
    setError(undefined);
    try {
      await fn();
    } catch (e) {
      setError(e as ApiError);
    }
  }

  return (
    <>
      <PageHeader title="Knowledge" subtitle="Documents are chunked and indexed in the background." />
      <ErrorBanner error={error ?? kbs.error} />
      <div className="mb-4 flex flex-wrap items-center gap-2">
        {kbs.data?.map((k) => (
          <button
            key={k.id}
            onClick={() => setKbId(k.id)}
            className={`rounded-full px-3 py-1 text-sm ${kbId === k.id ? "bg-slate-900 text-white" : "bg-white text-slate-700 border border-slate-300"}`}
          >
            {k.name}
          </button>
        ))}
        <input className={`${inputCls} max-w-48`} placeholder="new knowledge base" value={newKb} onChange={(e) => setNewKb(e.target.value)} />
        <Button
          variant="secondary"
          disabled={newKb.trim().length < 2}
          onClick={() =>
            guard(async () => {
              const kb = await api<KnowledgeBase>(`/orgs/${org}/knowledge-bases`, { method: "POST", body: { name: newKb } });
              setNewKb("");
              kbs.reload();
              setKbId(kb.id);
            })
          }
        >
          Create
        </Button>
      </div>
      {kbId && (
        <div className="grid gap-6 xl:grid-cols-2">
          <div>
            <Card title="Search">
              <div className="mb-3 flex gap-2">
                <input name="query" className={inputCls} value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Ask about your documents" />
                <select className="rounded border border-slate-300 px-2 text-sm" value={strategy} onChange={(e) => setStrategy(e.target.value)}>
                  <option value="hybrid">hybrid</option>
                  <option value="dense">dense</option>
                  <option value="keyword">keyword</option>
                </select>
                <Button
                  disabled={!query.trim()}
                  onClick={() =>
                    guard(async () => {
                      const r = await api<{ results: SearchHit[] }>(`/orgs/${org}/knowledge-bases/${kbId}/search`, {
                        method: "POST",
                        body: { query, strategy, top_k: 5 },
                      });
                      setHits(r.results);
                    })
                  }
                >
                  Search
                </Button>
              </div>
              {hits?.length === 0 && <p className="text-sm text-slate-500">No relevant passages.</p>}
              {hits?.map((h) => (
                <div key={h.chunk_id} className="mb-3 rounded border border-slate-100 p-2 text-sm">
                  <div className="mb-1 flex justify-between text-xs text-slate-500">
                    <span>
                      <b className="text-slate-800">{h.title}</b>
                      {h.section ? ` / ${h.section}` : ""}
                    </span>
                    <span className="font-mono">
                      {h.score.toFixed(4)} {Object.entries(h.sources).map(([k, v]) => `${k}#${v}`).join(" ")}
                    </span>
                  </div>
                  <p className="whitespace-pre-wrap text-slate-700">{h.text}</p>
                </div>
              ))}
            </Card>
            <Card title="Documents">
              <Table headers={["Title", "Status", "Chunks", "Attempts"]} empty={docs.data?.length === 0}>
                {docs.data?.map((d) => (
                  <tr key={d.id}>
                    <Td>{d.title}</Td>
                    <Td>
                      <Badge value={d.status} />
                      {d.last_error && <div className="text-xs text-red-700">{d.last_error}</div>}
                    </Td>
                    <Td>{d.chunk_count}</Td>
                    <Td>{d.attempts}</Td>
                  </tr>
                ))}
              </Table>
            </Card>
          </div>
          <Card title="Add document (text / Markdown)">
            <Field label="Title">
              <input className={inputCls} value={title} onChange={(e) => setTitle(e.target.value)} />
            </Field>
            <Field label="Content">
              <textarea className={`${inputCls} h-64 font-mono text-xs`} value={content} onChange={(e) => setContent(e.target.value)} />
            </Field>
            <Button
              disabled={!title.trim() || !content.trim()}
              onClick={() =>
                guard(async () => {
                  await api(`/orgs/${org}/knowledge-bases/${kbId}/documents`, { method: "POST", body: { title, content } });
                  setTitle("");
                  setContent("");
                  docs.reload();
                })
              }
            >
              Upload
            </Button>
          </Card>
        </div>
      )}
    </>
  );
}
