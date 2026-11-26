"""The Cyber Security toolkit: a curated catalog of open-source security tools.

These are external projects the app helps you install and point at your own lab
or authorized targets — MCP servers plug into Connectors, skill repos go to the
Marketplace, and standalone agents/CLIs come with a setup command. Nothing here
is bundled or run by the app itself; you install and authorize each tool.

Deliberately excluded: AI jailbreak / guardrail-bypass repositories. Owning a
lab doesn't turn those into app bug-hunting tools, so they're not listed."""
from __future__ import annotations

GH = "https://github.com/"

# cat: section in the Bug bounty view.
# kind: "mcp" -> add as a Connector; "skill" -> add as a Marketplace source; "tool" -> install + run yourself.
# connect: for kind "mcp", a ready-to-edit Connectors row ({"local": {...}} or {"remote": {...}}).
# setup: a copy-paste install/run command. repo: owner/repo on GitHub.
CATALOG = [
    # ---- Reverse engineering (featured in its own tab) ---------------------
    {"name": "REA — Reverse Engineer Anything", "repo": "morluto/rea", "cat": "Reverse engineering",
     "kind": "mcp", "featured": "rea",
     "blurb": "One MCP for reverse engineering across native binaries, JS/Electron apps, .NET, websites, Android and firmware. Analysis runs locally.",
     "setup": "npx rea-agents@latest setup",
     "connect": {"local": {"name": "rea", "command": "npx", "args": "-y rea-agents@latest",
                           "tip": "Run `npx rea-agents@latest setup` once first. Native decompilation needs Ghidra/IDA/Hopper installed."}}},

    # ---- Web & network MCP servers (plug into Connectors) ------------------
    {"name": "Burp Suite MCP (PortSwigger)", "repo": "PortSwigger/mcp-server", "cat": "Web & network MCP",
     "kind": "mcp", "blurb": "Official Burp Suite MCP server — drive Burp from the agent for authorized web-app testing.",
     "setup": "Install the MCP server extension from Burp's BApp Store, then enable it under Settings → MCP.",
     "connect": {"remote": {"name": "burp", "url": "http://127.0.0.1:9876/sse",
                            "tip": "Enable the MCP server in Burp (Settings → MCP) and confirm the URL/port it shows."}}},
    {"name": "BurpMCP-Ultra", "repo": "Cy-S3c/BurpMCP-Ultra", "cat": "Web & network MCP",
     "kind": "mcp", "blurb": "Extended Burp MCP server with a wider tool surface for agent-driven testing.",
     "setup": "git clone https://github.com/Cy-S3c/BurpMCP-Ultra  # then follow its README to load into Burp"},
    {"name": "BurpMcp", "repo": "swgee/BurpMcp", "cat": "Web & network MCP",
     "kind": "mcp", "blurb": "Burp Suite MCP extension exposing proxy history, repeater and scanner to an agent.",
     "setup": "git clone https://github.com/swgee/BurpMcp  # load the extension in Burp"},
    {"name": "Burp MCP Agents", "repo": "six2dez/burp-mcp-agents", "cat": "Web & network MCP",
     "kind": "mcp", "blurb": "Agent workflows built on the Burp MCP server for recon and triage.",
     "setup": "git clone https://github.com/six2dez/burp-mcp-agents"},
    {"name": "Pentest MCP", "repo": "DMontgomery40/pentest-mcp", "cat": "Web & network MCP",
     "kind": "mcp", "blurb": "MCP server wrapping common pentest utilities (nmap, dirb and friends).",
     "setup": "git clone https://github.com/DMontgomery40/pentest-mcp  # see README for the run command",
     "connect": {"local": {"name": "pentest", "command": "python", "args": "pentest_mcp.py",
                           "tip": "Clone the repo and point the command at its server entry file."}}},
    {"name": "Metasploit MCP", "repo": "GH05TCREW/MetasploitMCP", "cat": "Web & network MCP",
     "kind": "mcp", "blurb": "MCP bridge to the Metasploit Framework. Use only against hosts you're authorized to test.",
     "setup": "git clone https://github.com/GH05TCREW/MetasploitMCP  # needs msfrpcd running",
     "connect": {"local": {"name": "metasploit", "command": "python", "args": "MetasploitMCP.py",
                           "tip": "Start msfrpcd first; set the RPC password/host the server expects. Authorized targets only."}}},
    {"name": "Shodan MCP", "repo": "w0h1v/mcp-shodan", "cat": "Web & network MCP",
     "kind": "mcp", "blurb": "Query Shodan (host/service intelligence) from the agent. Needs a Shodan API key.",
     "setup": "git clone https://github.com/w0h1v/mcp-shodan",
     "connect": {"local": {"name": "shodan", "command": "python", "args": "server.py",
                           "tip": "Add your SHODAN_API_KEY in the environment box after cloning."}}},
    {"name": "OSINT MCP", "repo": "badchars/osint-mcp-server", "cat": "Web & network MCP",
     "kind": "mcp", "blurb": "Open-source-intelligence lookups (domains, emails, infra) exposed as MCP tools.",
     "setup": "git clone https://github.com/badchars/osint-mcp-server"},
    {"name": "Exploit-DB MCP", "repo": "Cyreslab-AI/exploitdb-mcp-server", "cat": "Web & network MCP",
     "kind": "mcp", "blurb": "Search the Exploit-DB database from the agent to match findings to known exploits.",
     "setup": "git clone https://github.com/Cyreslab-AI/exploitdb-mcp-server"},

    # ---- Autonomous pentest / bug-bounty agents ----------------------------
    {"name": "PentestGPT", "repo": "GreyDGL/PentestGPT", "cat": "Pentest & bug-bounty agents",
     "kind": "tool", "blurb": "LLM-guided penetration testing assistant that walks an engagement step by step.",
     "setup": "pip install pentestgpt"},
    {"name": "HexStrike AI", "repo": "0x4m4/hexstrike-ai", "cat": "Pentest & bug-bounty agents",
     "kind": "tool", "blurb": "Autonomous offensive-security agent that orchestrates many tools.",
     "setup": "git clone https://github.com/0x4m4/hexstrike-ai"},
    {"name": "PentAGI", "repo": "vxcontrol/pentagi", "cat": "Pentest & bug-bounty agents",
     "kind": "tool", "blurb": "Autonomous AI agents for complex penetration-testing tasks, self-hosted.",
     "setup": "git clone https://github.com/vxcontrol/pentagi"},
    {"name": "Nebula", "repo": "berylliumsec/nebula", "cat": "Pentest & bug-bounty agents",
     "kind": "tool", "blurb": "AI-powered assistant for ethical hacking and recon.",
     "setup": "pip install nebula-ai"},
    {"name": "hackingBuddyGPT", "repo": "ipa-lab/hackingBuddyGPT", "cat": "Pentest & bug-bounty agents",
     "kind": "tool", "blurb": "Research framework for LLM-driven privilege-escalation and testing in your lab.",
     "setup": "pip install hackingBuddyGPT"},
    {"name": "RAPTOR", "repo": "gadievron/raptor", "cat": "Pentest & bug-bounty agents",
     "kind": "tool", "blurb": "Recon and attack-path tooling for offensive engagements.",
     "setup": "git clone https://github.com/gadievron/raptor"},
    {"name": "BountyForge", "repo": "Gabson0x/bountyforge", "cat": "Pentest & bug-bounty agents",
     "kind": "tool", "blurb": "Bug-bounty automation pipeline for recon and triage.",
     "setup": "git clone https://github.com/Gabson0x/bountyforge"},
    {"name": "Bolt", "repo": "CyberStrikeus/bolt", "cat": "Pentest & bug-bounty agents",
     "kind": "tool", "blurb": "Offensive automation toolkit.",
     "setup": "git clone https://github.com/CyberStrikeus/bolt"},
    {"name": "Decepticon", "repo": "BitterSecurity/Decepticon", "cat": "Pentest & bug-bounty agents",
     "kind": "tool", "blurb": "Multi-agent red-team framework.",
     "setup": "git clone https://github.com/BitterSecurity/Decepticon"},
    {"name": "Agentic Bug Hunter", "repo": "Awarexone/Agentic-Bug-Hunter", "cat": "Pentest & bug-bounty agents",
     "kind": "tool", "blurb": "Agent that hunts for vulnerabilities across a target.",
     "setup": "git clone https://github.com/Awarexone/Agentic-Bug-Hunter"},
    {"name": "PentestAgent", "repo": "GH05TCREW/pentestagent", "cat": "Pentest & bug-bounty agents",
     "kind": "tool", "blurb": "Agent that automates pentest workflows end to end.",
     "setup": "git clone https://github.com/GH05TCREW/pentestagent"},
    {"name": "Pentest Swarm AI", "repo": "Armur-Ai/Pentest-Swarm-AI", "cat": "Pentest & bug-bounty agents",
     "kind": "tool", "blurb": "A swarm of cooperating agents for penetration testing.",
     "setup": "git clone https://github.com/Armur-Ai/Pentest-Swarm-AI"},
    {"name": "cain-agent", "repo": "cdxiaodong/cain-agent", "cat": "Pentest & bug-bounty agents",
     "kind": "tool", "blurb": "Security agent for automated offensive tasks.",
     "setup": "git clone https://github.com/cdxiaodong/cain-agent"},
    {"name": "DorkAgent", "repo": "yee-yore/DorkAgent", "cat": "Pentest & bug-bounty agents",
     "kind": "tool", "blurb": "LLM agent for Google-dorking based recon.",
     "setup": "git clone https://github.com/yee-yore/DorkAgent"},
    {"name": "pentest-harness", "repo": "S1N6H/pentest-harness", "cat": "Pentest & bug-bounty agents",
     "kind": "tool", "blurb": "Harness for running and evaluating pentest agents.",
     "setup": "git clone https://github.com/S1N6H/pentest-harness"},
    {"name": "reconFTW AI", "repo": "six2dez/reconftw_ai", "cat": "Pentest & bug-bounty agents",
     "kind": "tool", "blurb": "AI layer over reconFTW for recon analysis and reporting.",
     "setup": "git clone https://github.com/six2dez/reconftw_ai"},
    {"name": "Open Interpreter", "repo": "openinterpreter/open-interpreter", "cat": "Pentest & bug-bounty agents",
     "kind": "tool", "blurb": "General local code-execution agent; a flexible driver for custom tooling.",
     "setup": "pip install open-interpreter"},

    # ---- AI / LLM security (defensive red-teaming & guards) -----------------
    {"name": "garak (NVIDIA)", "repo": "NVIDIA/garak", "cat": "AI / LLM security",
     "kind": "tool", "blurb": "LLM vulnerability scanner — probes a model for jailbreaks, leakage and failures.",
     "setup": "pipx install garak"},
    {"name": "promptfoo", "repo": "promptfoo/promptfoo", "cat": "AI / LLM security",
     "kind": "tool", "blurb": "Test and red-team your own LLM apps; eval prompts and run attack suites.",
     "setup": "npx promptfoo@latest init"},
    {"name": "PyRIT (Microsoft)", "repo": "microsoft/PyRIT", "cat": "AI / LLM security",
     "kind": "tool", "blurb": "Python Risk Identification Toolkit for generative-AI red teaming.",
     "setup": "pip install pyrit"},
    {"name": "Giskard", "repo": "Giskard-AI/giskard-oss", "cat": "AI / LLM security",
     "kind": "tool", "blurb": "Open-source testing for ML and LLM systems (bias, robustness, security).",
     "setup": "pip install giskard"},
    {"name": "AI-Infra-Guard (Tencent)", "repo": "Tencent/AI-Infra-Guard", "cat": "AI / LLM security",
     "kind": "tool", "blurb": "Scanner for vulnerabilities in AI infrastructure and model-serving stacks.",
     "setup": "git clone https://github.com/Tencent/AI-Infra-Guard"},
    {"name": "AgentDojo", "repo": "ethz-spylab/agentdojo", "cat": "AI / LLM security",
     "kind": "tool", "blurb": "Benchmark for prompt-injection attacks and defenses against AI agents.",
     "setup": "pip install agentdojo"},
    {"name": "PurpleLlama (Meta)", "repo": "meta-llama/PurpleLlama", "cat": "AI / LLM security",
     "kind": "tool", "blurb": "Meta's trust-and-safety tools: CyberSecEval, Llama Guard and more.",
     "setup": "git clone https://github.com/meta-llama/PurpleLlama"},
    {"name": "LLM Guard", "repo": "protectai/llm-guard", "cat": "AI / LLM security",
     "kind": "tool", "blurb": "Security toolkit that filters prompts and outputs for LLM apps.",
     "setup": "pip install llm-guard"},
    {"name": "BurpGPT", "repo": "aress31/burpgpt", "cat": "AI / LLM security",
     "kind": "tool", "blurb": "Burp extension that uses an LLM to spot vulnerabilities in traffic.",
     "setup": "git clone https://github.com/aress31/burpgpt  # load the extension in Burp"},

    # ---- Skills & wordlists ------------------------------------------------
    {"name": "Trail of Bits skills", "repo": "trailofbits/skills", "cat": "Skills & wordlists",
     "kind": "skill", "blurb": "Security-engineering skills from Trail of Bits — add them to your Marketplace."},
    {"name": "Anthropic Cybersecurity Skills", "repo": "mukul975/Anthropic-Cybersecurity-Skills", "cat": "Skills & wordlists",
     "kind": "skill", "blurb": "A collection of cybersecurity skills for the agent."},
    {"name": "Transilience community tools", "repo": "transilienceai/communitytools", "cat": "Skills & wordlists",
     "kind": "skill", "blurb": "Community security tools and agent skills."},
    {"name": "AISecLists", "repo": "SecNode/AISecLists", "cat": "Skills & wordlists",
     "kind": "tool", "blurb": "Wordlists and payloads for AI/LLM security testing (SecLists-style).",
     "setup": "git clone https://github.com/SecNode/AISecLists"},
]

# repos that must never be surfaced here, even if asked (AI guardrail-bypass material)
EXCLUDED = {"elder-plinius/L1B3RT4S", "SnailSploit/Claude-Red"}

# skill repos we also register in the Marketplace by default so they show up there too
SKILL_SOURCES = [t["repo"] for t in CATALOG if t["kind"] == "skill"]


def _seen_unique():
    seen, out = set(), []
    for t in CATALOG:
        if t["repo"] in EXCLUDED or t["repo"] in seen:
            continue
        seen.add(t["repo"])
        out.append({**t, "url": GH + t["repo"]})
    return out


def catalog() -> dict:
    """The toolkit for the Cyber Security view: entries grouped by category, plus the REA feature."""
    items = _seen_unique()
    cats: dict[str, list] = {}
    rea = None
    for t in items:
        if t.get("featured") == "rea":
            rea = t
        cats.setdefault(t["cat"], []).append(t)
    order = ["Web & network MCP", "Pentest & bug-bounty agents", "AI / LLM security",
             "Skills & wordlists", "Reverse engineering"]
    groups = [{"name": c, "tools": cats[c]} for c in order if c in cats]
    return {"groups": groups, "rea": rea, "count": len(items), "excluded": sorted(EXCLUDED)}
