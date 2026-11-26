# Diagram types

One working example of each type. The examples are synthetic: names and figures are invented.

## flowchart

```mermaid
flowchart TD
  A[Request received] --> B{Signed in?}
  B -->|yes| C[Show the page]
  B -->|no| D[Ask for sign-in]
  D --> B
```

Directions: `TD` (top down), `LR` (left to right), `BT`, `RL`. Shapes: `[box]`, `(rounded)`, `{decision}`, `((circle))`, `[(database)]`. Group nodes with `subgraph Name` and `end`.

## sequenceDiagram

```mermaid
sequenceDiagram
  participant U as User
  participant S as Server
  U->>S: Sign in
  S-->>U: Session cookie
  Note over U,S: The cookie is sent with each request
```

`->>` is a solid arrow, `-->>` a dashed reply. Use `alt` and `else` for choices and `loop` for repeats, each closed by `end`.

## classDiagram

```mermaid
classDiagram
  class Account {
    +String name
    +deposit(amount)
  }
  class Savings {
    +Float rate
  }
  Account <|-- Savings
```

## stateDiagram-v2

```mermaid
stateDiagram-v2
  [*] --> Draft
  Draft --> Review: submit
  Review --> Approved: approve
  Review --> Draft: reject
  Approved --> [*]
```

## erDiagram

```mermaid
erDiagram
  CUSTOMER ||--o{ ORDER : places
  ORDER ||--|{ LINE : contains
  CUSTOMER {
    string name
    string email
  }
```

## gantt

```mermaid
gantt
  title Launch plan
  dateFormat YYYY-MM-DD
  section Build
  Design   :a1, 2026-03-01, 10d
  Develop  :a2, after a1, 20d
  section Release
  Test     :a3, after a2, 7d
```

## timeline

```mermaid
timeline
  title Product history
  2024 : Idea
  2025 : First release : Wider trial
  2026 : General release
```

## mindmap

```mermaid
mindmap
  root((Garden))
    Plants
      Herbs
      Vegetables
    Tools
      Spade
      Hose
```

## journey

```mermaid
journey
  title Signing up
  section Find
    Visit the site: 4: Visitor
  section Join
    Fill in the form: 3: Visitor
    Confirm by email: 2: Visitor
```

## pie

```mermaid
pie title Tickets by channel
  "Email" : 54
  "Chat" : 31
  "Phone" : 15
```

## gitGraph

```mermaid
gitGraph
  commit
  branch feature
  checkout feature
  commit
  checkout main
  merge feature
```

## quadrantChart

```mermaid
quadrantChart
  title Effort against value
  x-axis Low effort --> High effort
  y-axis Low value --> High value
  Quick win: [0.2, 0.8]
  Big project: [0.8, 0.7]
  Distraction: [0.7, 0.2]
```

## The one thing SPARK refuses

A settings directive is refused, so never write this:

```mermaid
%%{init: {"theme": "dark"}}%%  (refused: remove it)
flowchart TD
  A --> B
```
