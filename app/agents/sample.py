"""Sample input: a fictional kickoff meeting and Lumen's company rules.

The transcript deliberately contains things the agents must handle:
- a conflict: go-live 27 Nov (contract) vs 20 Nov (client wish)          L2, L10, L11
- a missing owner: store-manager training                                 L9
- an external person who cannot own a task: Alex (client IT)              L8
- an offer that breaks a rule: weekend migration on Sat 14 Nov            L12
- customer data, which triggers the security-review rule                  L5, L6
"""

SAMPLE_TITLE = "Northwind Retail: sales dashboard kickoff"

SAMPLE_TRANSCRIPT = """\
Meera (Lumen, PM): Thanks all. This is the kickoff for Northwind Retail's sales dashboard rollout.
Sam (Northwind): Our goal is a daily sales dashboard for all 40 stores, live before our Black Friday review on 27 November.
Meera (Lumen, PM): Decision: we'll build it in Power BI, on top of a new Snowflake warehouse.
Daniel (Lumen, data): I can own the Snowflake setup and the nightly load from their POS system.
Sam (Northwind): The POS export contains customer emails, so it counts as customer data.
Arjun (Lumen, security): Then nothing touches production customer data before a security review. That's our policy.
Priya (Lumen, BI): I'll take the dashboard design. I need the store hierarchy file from Northwind first.
Sam (Northwind): Our IT lead, Alex, will send the store hierarchy file, probably next week.
Meera (Lumen, PM): We also need a training session for the store managers. No owner yet.
Sam (Northwind): Actually, can we aim for go-live on 20 November instead? Leadership wants it earlier.
Meera (Lumen, PM): Let's note that as open; 27 November is what's in the contract.
Daniel (Lumen, data): If it helps, I could do the data migration over the weekend of 14 November.
Priya (Lumen, BI): Dashboards must load in under 3 seconds on store tablets.
Sam (Northwind): And everything has to stay within our 40k budget.
Meera (Lumen, PM): Agreed. Next check-in Thursday 12 November.
"""

SAMPLE_RULES = """\
company: Lumen Analytics Co.
meeting_date: 2026-11-05
contract_go_live: 2026-11-27
max_tasks_per_owner: 3
max_review_rounds: 3

team:
  - name: Meera Iyer
    role: project manager
    tags: [pm]
  - name: Priya Nair
    role: BI lead
  - name: Daniel Brooks
    role: data engineer
  - name: Arjun Rao
    role: security lead
    tags: [security]

rules:
  - id: R1
    text: Every task has exactly one owner from the team list. Client staff cannot own tasks;
      track what we need from the client as an open question or a dependency note.
  - id: R2
    text: Deadlines fall on working days (Monday to Friday) only. No weekend work.
  - id: R3
    text: Any task that touches customer data must depend on a security review task owned by the
      security lead.
  - id: R4
    text: Client-facing deliverables (dashboards, training) need a sign-off task owned by the project
      manager that depends on them.
  - id: R5
    text: No owner has more than max_tasks_per_owner tasks.
  - id: R6
    text: Deadlines fall between the meeting date and the contract go-live, and never before the
      deadline of a task they depend on. A deadline is either stated in the meeting (cite it) or
      marked as a recommendation.
"""
