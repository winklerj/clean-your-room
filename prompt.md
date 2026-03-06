Study the existing specs/*

Identify one specification that still needs created for the clean room deep research specifications and create the specification file.

Focus on ONE specification
Include:
- Provable Properties Catalog: Which invariants, safety properties, and correctness guarantees must be formally verified, not just tested? Distinguish between properties that should be proven (critical path, security boundaries, financial calculations) and properties where test coverage is sufficient (UI formatting, logging, non-critical defaults).
- Purity Boundary Map: A clear architectural separation between the deterministic, side-effect-free core (where formal verification can operate) and the effectful shell (I/O, network, database, user interaction). It dictates module boundaries, dependency direction, and how state flows through the system. The pure core must be designed so that verification tools can reason about it without mocking the entire universe.
- Verification Tooling Selection: Based on the language and the properties to be proven, the Builder selects the appropriate formal verification stack (Kani for Rust, CBMC for C/C++, Dafny, TLA+ for distributed systems, Antithesis Bombadil for frontend, Lean 4 for system verification, Promela, Raft, Paxos, Alloy, PRISM, etc.) and identifies any constraints these tools impose on code structure.
- Propery Specifications: Where possible, draft the actual formal property definitions (e.g., Kani proof harnesses, Dafny contracts, TLA+ invariants) alongside the behavioral spec. These aren't implementation. They are the formal expression of what the spec already says in natural language. They serve as a second, mathematically precise encoding of the requirements.
