# Two-minute assistant demo

**Setup, before the demo.**

- Stack running (`docker compose up --build --wait`, or `scripts/e2e_server.py` + `npm start`).
- Signed in, organization created.
- **Assistant → Load demo content** clicked, then a short wait while 20 documents index.
  Check this under **Knowledge**.
- The model selector shows the model you'll use. With no API key that's
  `fixture:policy-v1`, and you should **say so out loud**: it is a rule-based test policy,
  not a language model. (This project's budget is $0, so the fixture policy is the only model offered.)

| Time | Do | Say |
|---|---|---|
| 0:00–0:15 | Show the Assistant page | "Support assistant for a fictional home-services company. It answers from the company's documents, checks tickets, and can draft tickets, but it can't open one without a person." |
| 0:15–0:40 | Ask: *How many days do I have to ask for a refund?* Open **Sources** and **Tool activity** | "The model called `search_documents` through Claude's native tool use. The answer cites `HP-POL-02#…`, and the server checked that this chunk was actually retrieved in this run. Fabricated IDs are stripped." |
| 0:40–0:55 | Ask: *What's the status of ticket T-1A2B3C4D?* | "Different tool: `get_ticket_status`. Arguments are validated in code, and the lookup is scoped to this organization." |
| 0:55–1:25 | Ask: *Please open a ticket: my kitchen tap has been dripping since Monday.* Show the amber draft card. Edit the subject, **Save**, then **Approve** | "The model can only create a *draft*. Approval needs a staff permission and is bound to the exact text I reviewed. If someone edits it, the old approval is refused. Approving twice can't create two tickets." Point at the ticket ID. |
| 1:25–1:45 | Ask: *Do you clean swimming pools?* | "Nothing in the documents. It says so, offers a next step, and the run is marked *insufficient evidence*." |
| 1:45–2:00 | Point at the run summary line | "Every run records model calls, retries, tokens, an estimated cost from list prices, and latency split into retrieval and tools. The same runs feed the eval suite: 30 dev cases plus 10 held out." |

**If asked about results:**

- **Live model scores have not been run.** No API key was available. The eval harness
  reports those runs as NOT RUN (the project budget is $0) rather than inventing numbers.
- **What was measured:** retrieval (hit rate @3 of 11/12 → 12/12 with semantic dense
  retrieval) and the harness, using the fixture.
