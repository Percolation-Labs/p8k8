# Mermaid Syntax Reference

You MUST follow this reference exactly when generating mermaid diagrams. Copy the patterns below.

## Code Fence Format

Every mermaid diagram uses this exact structure:

    ```mermaid
    <diagram-type-keyword>
        <content>
    ```

The code fence language is always `mermaid`. The diagram type keyword is always the first line inside the block.

## Common Diagram Examples — Copy These Exactly

### Bar/Line Chart (keyword: xychart-beta)

```mermaid
xychart-beta
    title "Sales by Quarter"
    x-axis [Q1, Q2, Q3, Q4]
    y-axis "Revenue (k)" 0 --> 100
    bar [25, 40, 38, 65]
    line [20, 35, 32, 58]
```

### Pie Chart (keyword: pie)

```mermaid
pie title "Traffic Sources"
    "Organic Search" : 45
    "Direct" : 25
    "Social Media" : 15
    "Referral" : 10
    "Email" : 5
```

### Flowchart (keyword: graph LR or graph TD)

```mermaid
graph LR
    A[Start] --> B{Decision}
    B -->|Yes| C[Action 1]
    B -->|No| D[Action 2]
```

## Diagram Type Keywords

The first line inside the mermaid block MUST be one of these exact keywords. Anything else fails to render.

| Keyword | Type |
|---------|------|
| `graph LR` / `graph TD` / `flowchart LR` | Flowchart |
| `sequenceDiagram` | Sequence diagram |
| `classDiagram` | Class diagram |
| `stateDiagram-v2` | State diagram |
| `erDiagram` | Entity-relationship |
| `mindmap` | Mind map |
| `timeline` | Timeline |
| `pie` | Pie chart |
| `xychart-beta` | Bar/line charts (NOT "bar", "chart", or "xychart") |
| `quadrantChart` | Quadrant/2x2 matrix |
| `gantt` | Gantt chart |
| `block-beta` | Block/architecture diagram |
| `sankey-beta` | Sankey flow diagram |
| `gitGraph` | Git branch visualization |

## Flowchart

Direction keywords: `TB` (top-bottom), `TD` (top-down), `BT`, `LR` (left-right), `RL`

```mermaid
graph LR
    A[Rectangle] --> B(Rounded)
    B --> C{Diamond}
    C -->|Yes| D[[Subroutine]]
    C -->|No| E[(Database)]
    D --> F((Circle))
    E --> F
    F --> G>Asymmetric]
    G --> H{{Hexagon}}
    H --> I[/Parallelogram/]
    I --> J[\Reverse Parallelogram\]
    J --> K[/Trapezoid\]
```

### Subgraphs
```mermaid
graph TB
    subgraph Frontend
        A[React App] --> B[API Client]
    end
    subgraph Backend
        C[FastAPI] --> D[Database]
    end
    B --> C
```

### Link styles
```
A --> B           %% Arrow
A --- B           %% Line
A -.-> B          %% Dotted arrow
A ==> B           %% Thick arrow
A --text--> B     %% Arrow with text
A -->|text| B     %% Arrow with text (alt)
A ~~~ B           %% Invisible link
```

### Styling
```mermaid
graph LR
    A:::highlight --> B
    classDef highlight fill:#f9f,stroke:#333,stroke-width:2px
    style B fill:#bbf,stroke:#333
```

## Sequence Diagram

```mermaid
sequenceDiagram
    actor U as User
    participant C as Client
    participant S as Server
    participant DB as Database

    U->>C: Click button
    activate C
    C->>+S: POST /api/data
    S->>+DB: SELECT * FROM items
    DB-->>-S: Results
    S-->>-C: 200 OK {items}
    deactivate C
    C->>U: Display items

    Note over C,S: Authentication flow
    Note right of S: Validate JWT

    alt Success
        S-->>C: 200 OK
    else Unauthorized
        S-->>C: 401 Error
    end

    loop Every 30s
        C->>S: Heartbeat
        S-->>C: ACK
    end

    par Parallel requests
        C->>S: GET /users
    and
        C->>S: GET /posts
    end

    rect rgb(200, 220, 255)
        Note over C,S: Highlighted section
        C->>S: Important call
    end

    critical Establish connection
        C->>S: Connect
    option Network timeout
        C->>C: Retry
    option Server down
        C->>U: Show error
    end

    break When rate limited
        S-->>C: 429 Too Many Requests
    end
```

### Arrow types
```
->>   Solid arrow (request)
-->>  Dashed arrow (response)
-)    Open arrow (async)
--)   Dashed open arrow
-x    Cross (lost message)
--x   Dashed cross
```

## Class Diagram

```mermaid
classDiagram
    class Animal {
        <<abstract>>
        +String name
        +int age
        #String species
        -UUID id
        +makeSound()* str
        +move(int distance) void
        +getInfo() String
    }

    class Dog {
        +String breed
        +fetch() void
        +makeSound() str
    }

    class Cat {
        +bool isIndoor
        +purr() void
        +makeSound() str
    }

    class Owner {
        +String name
        +List~Animal~ pets
        +adopt(Animal a) void
    }

    Animal <|-- Dog : extends
    Animal <|-- Cat : extends
    Owner "1" --> "*" Animal : owns
    Dog ..> Food : depends on
    Cat ..|> Serializable : implements

    class Food {
        <<enumeration>>
        KIBBLE
        WET
        RAW
    }

    note for Animal "Base class for all animals"
```

### Relationships
```
<|--   Inheritance
*--    Composition
o--    Aggregation
-->    Association
--     Link (solid)
..>    Dependency
..|>   Realization/Implementation
```

### Cardinality
```
"1"    Exactly one
"0..1" Zero or one
"1..*" One or more
"*"    Many
"n"    N instances
```

## State Diagram

```mermaid
stateDiagram-v2
    [*] --> Idle
    Idle --> Processing : submit
    Processing --> Review : auto_check_pass
    Processing --> Failed : auto_check_fail
    Review --> Approved : approve
    Review --> Rejected : reject
    Approved --> [*]
    Rejected --> Idle : resubmit
    Failed --> Idle : retry

    state Processing {
        [*] --> Validating
        Validating --> Transforming : valid
        Validating --> [*] : invalid
        Transforming --> [*]
    }

    state fork_state <<fork>>
    Approved --> fork_state
    fork_state --> NotifyUser
    fork_state --> UpdateDB

    state join_state <<join>>
    NotifyUser --> join_state
    UpdateDB --> join_state
    join_state --> Complete

    state Review {
        [*] --> Pending
        Pending --> InReview : assign
        InReview --> Decision : complete
    }

    note right of Processing : May take up to 5 minutes
    note left of Idle : Initial state
```

## Entity-Relationship Diagram

```mermaid
erDiagram
    USER {
        uuid id PK
        string email UK
        string name
        timestamp created_at
    }
    ORGANIZATION {
        uuid id PK
        string name
        string plan
    }
    PROJECT {
        uuid id PK
        string name
        uuid org_id FK
        timestamp created_at
    }
    TASK {
        uuid id PK
        string title
        string status
        uuid project_id FK
        uuid assignee_id FK
    }
    COMMENT {
        uuid id PK
        text body
        uuid task_id FK
        uuid author_id FK
    }

    ORGANIZATION ||--o{ USER : "has members"
    ORGANIZATION ||--o{ PROJECT : contains
    PROJECT ||--o{ TASK : contains
    USER ||--o{ TASK : "assigned to"
    TASK ||--o{ COMMENT : has
    USER ||--o{ COMMENT : writes
```

### Relationship types
```
||--||   Exactly one to exactly one
||--o{   One to zero or more
||--|{   One to one or more
}o--o{   Zero or more to zero or more
```

### Attribute keys
```
PK   Primary key
FK   Foreign key
UK   Unique key
```

## Mindmap

```mermaid
mindmap
  root((Machine Learning))
    Supervised
      Classification
        SVM
        Random Forest
        Neural Networks
      Regression
        Linear
        Polynomial
    Unsupervised
      Clustering
        K-Means
        DBSCAN
      Dimensionality Reduction
        PCA
        t-SNE
    Reinforcement
      Q-Learning
      Policy Gradient
      Actor-Critic
```

Node shapes:
- `((text))` — circle (root)
- `(text)` — rounded rectangle
- `[text]` — square
- `))text((` — bang
- `)text(` — cloud
- `{{text}}` — hexagon

## Timeline

```mermaid
timeline
    title History of Web Frameworks
    section Server-Side
        2004 : Ruby on Rails
        2005 : Django
        2010 : Express.js
        2018 : FastAPI
    section Client-Side
        2010 : AngularJS
        2013 : React
        2014 : Vue.js
        2016 : Angular 2+
    section Full-Stack
        2016 : Next.js
        2020 : Remix
        2023 : Astro
```

## XY Chart (Bar and Line Charts)

IMPORTANT: The keyword is `xychart-beta` — NOT "bar", "chart", or "xychart".

```mermaid
xychart-beta
    title "Sales by Quarter"
    x-axis [Q1, Q2, Q3, Q4]
    y-axis "Revenue (k)" 0 --> 100
    bar [25, 40, 38, 65]
    line [20, 35, 32, 58]
```

### Horizontal orientation
```mermaid
xychart-beta horizontal
    title "Team Sizes"
    x-axis [Engineering, Design, Product, Marketing]
    bar [45, 12, 8, 15]
```

### Syntax
- `xychart-beta` or `xychart-beta horizontal` — starts the chart
- `title "Chart Title"` — optional title (quotes required for multi-word)
- `x-axis [cat1, cat2, cat3]` — categorical x-axis labels
- `x-axis "Title" min --> max` — numeric x-axis range
- `y-axis "Title" min --> max` — numeric y-axis range (or auto-range without min/max)
- `bar [val1, val2, val3]` — bar series
- `line [val1, val2, val3]` — line series
- Multiple `bar` and `line` series can be combined in one chart

## Sankey Diagram

IMPORTANT: The keyword is `sankey-beta`. Uses CSV format: source, target, value.

```mermaid
sankey-beta
Budget,Engineering,45
Budget,Marketing,25
Budget,Operations,30
Engineering,Frontend,20
Engineering,Backend,25
Marketing,Digital,15
Marketing,Events,10
```

## Quadrant Chart

```mermaid
quadrantChart
    title Effort vs Impact
    x-axis Low Effort --> High Effort
    y-axis Low Impact --> High Impact
    quadrant-1 Do First
    quadrant-2 Schedule
    quadrant-3 Delegate
    quadrant-4 Eliminate
    Feature A: [0.8, 0.9]
    Feature B: [0.2, 0.8]
    Feature C: [0.7, 0.3]
    Feature D: [0.2, 0.2]
    Feature E: [0.5, 0.6]
```

## Block Diagram

```mermaid
block-beta
    columns 3
    Frontend:3
    space
    block:API
        columns 2
        Auth Router
    end
    space
    DB[("PostgreSQL")]:2 Cache[("Redis")]
```

## Pie Chart

```mermaid
pie title Traffic Sources
    "Organic Search" : 45
    "Direct" : 25
    "Social Media" : 15
    "Referral" : 10
    "Email" : 5
```

## Git Graph

```mermaid
gitGraph
    commit
    commit
    branch feature
    checkout feature
    commit
    commit
    checkout main
    merge feature
    commit
```

## Tips

- Keep node labels short (1-4 words). Move details to notes or descriptions.
- Use subgraphs/groups to organize complex diagrams.
- Color and style sparingly — only to highlight key elements.
- Test diagram renders before saving — common errors include missing quotes around labels with spaces and mismatched brackets.
- For flowcharts with many nodes, prefer LR (left-right) direction for readability.
- Sequence diagrams: use `activate`/`deactivate` to show lifelines clearly.
- Use `%%` for comments in Mermaid source.
