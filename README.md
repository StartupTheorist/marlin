# Marlin

A Claude Code plugin that keeps your local domain-awareness skill up to date.

## Install

```
/plugin marketplace add StartupTheorist/marlin
/plugin install marlin@marlin
```

Then connect the data connector per your onboarding instructions.

## Cowork setup

In Cowork, code runs in a sandbox that blocks outbound network by default, so the sync needs the data host allowed:

Settings -> Capabilities -> Code execution -> *Allow network egress* -> *Additional allowed domains* -> add the host from your onboarding instructions (keep "Package managers only"). Then start a new session.

Without this, the sync reports that network egress is blocked.
