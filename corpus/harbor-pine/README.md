# Harbor & Pine Home Services: demo corpus

**Everything here is fictional demo content.** Harbor & Pine is not a real company. The
prices, policies, places and phone number were written for the SolutionForge assistant demo
and its evaluations. Every file is labelled `demo: true`.

| Folder | Contents | Collection |
|---|---|---|
| `docs/` | 20 versioned documents: services (`HP-SVC-*`), FAQs (`HP-FAQ-*`), policies (`HP-POL-*`), operating procedures (`HP-SOP-*`) | `core` |
| `injection/` | 5 prompt-injection documents (`HP-INJ-*`): plausible pages that carry instructions aimed at the assistant | `injection` |
| `tickets.json` | 4 demo support tickets with fixed IDs | n/a |

The injection documents are **isolated**. They are never loaded into the demo knowledge
base. They go only into a separate "injection test" knowledge base (the core documents plus
these five), which the prompt-injection eval cases use.

## Front matter

```yaml
doc_id: HP-POL-02       # stable ID; source references are DOC_ID#chunk_ordinal
title: Refund policy
type: policy            # service | faq | policy | procedure
version: "3.0"
collection: core        # core | injection
provenance: written for the SolutionForge demo (fictional company)
demo: true
```

Re-ingestion is safe. A document whose content has not changed is skipped. A changed
document replaces the previous version with the same `doc_id`, so there are never two live
copies.
