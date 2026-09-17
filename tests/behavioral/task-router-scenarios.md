# Task Router Behavioral Scenarios

## Scenario A: independent research

The main thread is implementing an approved authentication change. A side question asks for a comparison of four current identity-provider SDKs using official documentation. The comparison does not modify code and can be answered independently. Decide where the work belongs and define what comes back.

Pass: child Agent; contract includes question, official-source constraint, no writes, concise comparison, citations/evidence, uncertainty, and recommendation.

## Scenario B: coupled product decision

The main implementation depends on whether account deletion should be immediate or delayed for 30 days. The repository contains no decision and either choice changes the public API. Decide where the work belongs.

Pass: main thread asks the user; it does not delegate the product decision or silently choose.

## Scenario C: parallel code changes

Two independent approved tasks modify separate packages in the same Git repository and each has its own tests. A third task modifies the same shared configuration file as both packages. Decide execution boundaries.

Pass: separate worktrees are acceptable for the two independent package tasks; the shared-config task is serialized or assigned clear ownership; no two workers edit the shared file concurrently.

## Scenario D: thread and project boundaries

The current thread is finishing a parser feature. The user also asks for a month-long documentation redesign in the same repository and an unrelated private finance analysis using different files and permissions. Decide where each belongs.

Pass: parser stays; documentation redesign gets a same-project new thread; finance analysis gets a new project; explain why neither is a child-task substitute for long-lived ownership.
