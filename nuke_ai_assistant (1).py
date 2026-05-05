# ============================================================
#  NUKE AI ASSISTANT  –  Conversational + Smart Execution
#  Drop into Nuke Script Editor and run:  show_nuke_ai_panel()
#  Requires: Ollama running locally  (ollama serve)
# ============================================================

import json, os, re, requests
from datetime import datetime

try:
    import nuke
    from PySide2 import QtWidgets, QtCore, QtGui
    NUKE_ENV = True
except ImportError:
    NUKE_ENV = False


# ─────────────────────────────────────────────────────────────
#  NUKE DATA LAYER  –  raw getters, no formatting
# ─────────────────────────────────────────────────────────────

class NukeData:
    """Pure data access to the Nuke session. No AI, no formatting."""

    @staticmethod
    def all_nodes():
        return nuke.allNodes() if NUKE_ENV else []

    @staticmethod
    def nodes_by_type():
        counts = {}
        for n in NukeData.all_nodes():
            t = n.Class()
            counts[t] = counts.get(t, 0) + 1
        return counts

    @staticmethod
    def nodes_of_type(node_type):
        norm = node_type.lower()
        return [n for n in NukeData.all_nodes() if n.Class().lower() == norm]

    @staticmethod
    def node_names_of_type(node_type):
        return [n.name() for n in NukeData.nodes_of_type(node_type)]

    @staticmethod
    def missing_files():
        missing = []
        for n in NukeData.all_nodes():
            if n.Class() in ("Read", "ReadGeo", "Camera", "Write"):
                if "file" in n.knobs():
                    path = n["file"].value()
                    if path:
                        try:
                            resolved = nuke.filename(n)
                            if resolved and not os.path.exists(resolved):
                                missing.append({"node": n.name(), "path": path})
                        except Exception:
                            pass
        return missing

    @staticmethod
    def error_nodes():
        return [{"node": n.name(), "error": n.error()}
                for n in NukeData.all_nodes() if n.hasError()]

    @staticmethod
    def disabled_nodes():
        return [n.name() for n in NukeData.all_nodes()
                if n.knob("disable") and n["disable"].value()]

    @staticmethod
    def script_info():
        if not NUKE_ENV:
            return {}
        path  = nuke.root().name()
        fps   = nuke.root()["fps"].value()
        first = int(nuke.root()["first_frame"].value())
        last  = int(nuke.root()["last_frame"].value())
        return {"path": path, "fps": fps, "first_frame": first, "last_frame": last}

    @staticmethod
    def node_knobs(name):
        if not NUKE_ENV:
            return None
        node = nuke.toNode(name)
        if not node:
            return None
        out = {}
        for k in node.allKnobs():
            try:
                out[k.name()] = k.value()
            except Exception:
                out[k.name()] = "<complex>"
        return out

    @staticmethod
    def node_connections(name):
        if not NUKE_ENV:
            return None
        node = nuke.toNode(name)
        if not node:
            return None
        inputs  = [node.input(i).name() if node.input(i) else None for i in range(node.inputs())]
        outputs = [d.name() for d in node.dependent()]
        return {"inputs": inputs, "outputs": outputs}

    @staticmethod
    def heavy_nodes():
        heavy = []
        for n in NukeData.all_nodes():
            if n.Class() == "Blur" and "size" in n.knobs():
                if n["size"].value() > 50:
                    heavy.append({"node": n.name(), "reason": f"Blur size {n['size'].value()}"})
            if n.Class() in ("Denoise","Defocus","ZDefocus","VectorBlur",
                             "MotionBlur2D","Kronos","DeepToImage"):
                heavy.append({"node": n.name(), "reason": f"Heavy class: {n.Class()}"})
        return heavy


# ─────────────────────────────────────────────────────────────
#  NUKE ACTION LAYER  –  modifications
# ─────────────────────────────────────────────────────────────

class NukeActions:

    @staticmethod
    def create_node(node_type, name=None, input_node=None, knobs=None):
        aliases = {"Backdrop": "BackdropNode", "Merge": "Merge2", "Shuffle": "Shuffle2"}
        cls  = aliases.get(node_type, node_type)
        node = getattr(nuke.nodes, cls)()
        if name:
            node["name"].setValue(name)
        if input_node:
            src = nuke.toNode(input_node)
            if src:
                node.setInput(0, src)
        for k, v in (knobs or {}).items():
            if k in node.knobs():
                try:
                    node[k].setValue(v)
                except Exception:
                    pass
        return node.name()

    @staticmethod
    def set_knob(node_name, knob, value):
        node = nuke.toNode(node_name)
        if not node or knob not in node.knobs():
            return False
        node[knob].setValue(value)
        return True

    @staticmethod
    def batch_set(node_type, knobs):
        changed = []
        for n in NukeData.nodes_of_type(node_type):
            for k, v in knobs.items():
                if k in n.knobs():
                    try:
                        n[k].setValue(v)
                        changed.append(n.name())
                    except Exception:
                        pass
        return list(set(changed))

    @staticmethod
    def delete_node(name):
        node = nuke.toNode(name)
        if node:
            nuke.delete(node)
            return True
        return False

    @staticmethod
    def rename_node(old_name, new_name):
        node = nuke.toNode(old_name)
        if node:
            node["name"].setValue(new_name)
            return True
        return False

    @staticmethod
    def connect_nodes(src_name, dst_name, input_index=0):
        src = nuke.toNode(src_name)
        dst = nuke.toNode(dst_name)
        if src and dst:
            dst.setInput(input_index, src)
            return True
        return False

    @staticmethod
    def update_paths(old_prefix, new_prefix):
        updated = []
        for n in NukeData.all_nodes():
            if "file" in n.knobs():
                p = n["file"].value()
                if p and p.startswith(old_prefix):
                    n["file"].setValue(p.replace(old_prefix, new_prefix, 1))
                    updated.append(n.name())
        return updated

    @staticmethod
    def snapshot(label):
        script_path = nuke.root().name()
        if not script_path or script_path == "Root":
            return None
        snap_dir = os.path.join(os.path.dirname(script_path), "snapshots")
        os.makedirs(snap_dir, exist_ok=True)
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(snap_dir, f"{label}_{ts}.nk")
        nuke.scriptSave(path)
        return path

    @staticmethod
    def organize_by_type():
        by_type = {}
        for n in NukeData.all_nodes():
            by_type.setdefault(n.Class(), []).append(n)
        y = 0
        for t, nodes in sorted(by_type.items()):
            for i, n in enumerate(nodes):
                n.setXYpos(i * 160, y)
            y += 160

    @staticmethod
    def create_smart_setup(pattern, source_name):
        src = nuke.toNode(source_name)
        if not src:
            return []
        chains = {
            "beauty_comp":    [("Grade","Grade",{}),("Blur","Blur",{"size":1}),("Sharpen","Sharpen",{})],
            "color_correct":  [("ColorCorrect","CC",{}),("Saturation","Sat",{}),("HueCorrect","Hue",{})],
            "denoise_sharpen":[("Denoise","Denoise",{}),("Sharpen","Sharpen",{})],
        }
        specs = chains.get(pattern)
        if not specs:
            return []
        prev    = src
        created = []
        for cls, suffix, knobs in specs:
            name = NukeActions.create_node(cls, f"{source_name}_{suffix}", prev.name(), knobs)
            created.append(name)
            prev = nuke.toNode(name)
        return created


# ─────────────────────────────────────────────────────────────
#  TOOL DEFINITIONS  –  what the LLM can call
# ─────────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "get_script_stats",
        "description": (
            "Returns total node count AND a full breakdown of counts per node type. "
            "Use ONLY when the user wants a complete overview or breakdown of ALL node types. "
            "Do NOT use this just to count one specific type."
        ),
        "input_schema": {"type":"object","properties":{},"required":[]}
    },
    {
        "name": "get_nodes_of_type",
        "description": (
            "Returns the names and count of nodes matching a specific type. "
            "Use this when the user asks how many of a specific node type exist, "
            "or wants to list all nodes of one type (e.g. 'how many Read nodes', "
            "'list all Blur nodes', 'name all Grade nodes')."
        ),
        "input_schema": {
            "type":"object",
            "properties": {
                "node_type": {
                    "type":"string",
                    "description":"Exact Nuke class name e.g. Read, Blur, Grade, Merge2, Roto"
                }
            },
            "required":["node_type"]
        }
    },
    {
        "name": "get_missing_files",
        "description": "Find all Read/Write nodes whose file paths do not exist on disk.",
        "input_schema": {"type":"object","properties":{},"required":[]}
    },
    {
        "name": "get_error_nodes",
        "description": "Find all nodes currently showing errors in the Nuke session.",
        "input_schema": {"type":"object","properties":{},"required":[]}
    },
    {
        "name": "get_disabled_nodes",
        "description": "List all nodes that are currently disabled.",
        "input_schema": {"type":"object","properties":{},"required":[]}
    },
    {
        "name": "get_script_info",
        "description": "Get the script file path, FPS, and frame range.",
        "input_schema": {"type":"object","properties":{},"required":[]}
    },
    {
        "name": "get_node_knobs",
        "description": "Get all knob values for a specific named node.",
        "input_schema": {
            "type":"object",
            "properties":{"node_name":{"type":"string"}},
            "required":["node_name"]
        }
    },
    {
        "name": "get_node_connections",
        "description": "Get input and output connections for a specific named node.",
        "input_schema": {
            "type":"object",
            "properties":{"node_name":{"type":"string"}},
            "required":["node_name"]
        }
    },
    {
        "name": "get_heavy_nodes",
        "description": "Find performance-heavy nodes: large Blurs, Denoise, Defocus, VectorBlur, etc.",
        "input_schema": {"type":"object","properties":{},"required":[]}
    },
    {
        "name": "create_node",
        "description": "Create a new node in the Nuke script.",
        "input_schema": {
            "type":"object",
            "properties": {
                "node_type":  {"type":"string","description":"Nuke class name e.g. Blur, Grade, Read"},
                "name":       {"type":"string","description":"Optional node name"},
                "input_node": {"type":"string","description":"Name of node to connect as input"},
                "knobs":      {"type":"object","description":"Knob name to value pairs"}
            },
            "required":["node_type"]
        }
    },
    {
        "name": "set_knob",
        "description": "Set a specific knob value on a named node.",
        "input_schema": {
            "type":"object",
            "properties": {
                "node_name": {"type":"string"},
                "knob":      {"type":"string"},
                "value":     {}
            },
            "required":["node_name","knob","value"]
        }
    },
    {
        "name": "batch_set",
        "description": "Set knob values on all nodes of a given type at once.",
        "input_schema": {
            "type":"object",
            "properties": {
                "node_type": {"type":"string"},
                "knobs":     {"type":"object"}
            },
            "required":["node_type","knobs"]
        }
    },
    {
        "name": "delete_node",
        "description": "Delete a specific node from the script by name.",
        "input_schema": {
            "type":"object",
            "properties":{"node_name":{"type":"string"}},
            "required":["node_name"]
        }
    },
    {
        "name": "rename_node",
        "description": "Rename a node.",
        "input_schema": {
            "type":"object",
            "properties": {
                "old_name": {"type":"string"},
                "new_name": {"type":"string"}
            },
            "required":["old_name","new_name"]
        }
    },
    {
        "name": "connect_nodes",
        "description": "Connect two nodes together (pipe source into destination).",
        "input_schema": {
            "type":"object",
            "properties": {
                "source":      {"type":"string"},
                "destination": {"type":"string"},
                "input_index": {"type":"integer","default":0}
            },
            "required":["source","destination"]
        }
    },
    {
        "name": "update_file_paths",
        "description": "Replace a path prefix across all Read/Write nodes.",
        "input_schema": {
            "type":"object",
            "properties": {
                "old_prefix": {"type":"string"},
                "new_prefix": {"type":"string"}
            },
            "required":["old_prefix","new_prefix"]
        }
    },
    {
        "name": "save_snapshot",
        "description": "Save a versioned snapshot of the current script to disk.",
        "input_schema": {
            "type":"object",
            "properties":{"label":{"type":"string"}},
            "required":["label"]
        }
    },
    {
        "name": "organize_by_type",
        "description": "Auto-arrange all nodes in the node graph, grouped by type.",
        "input_schema": {"type":"object","properties":{},"required":[]}
    },
    {
        "name": "create_smart_setup",
        "description": (
            "Create a pre-built node chain after a source node. "
            "Patterns: beauty_comp, color_correct, denoise_sharpen."
        ),
        "input_schema": {
            "type":"object",
            "properties": {
                "pattern":     {"type":"string","enum":["beauty_comp","color_correct","denoise_sharpen"]},
                "source_node": {"type":"string"}
            },
            "required":["pattern","source_node"]
        }
    },
]


# ─────────────────────────────────────────────────────────────
#  TOOL EXECUTOR
# ─────────────────────────────────────────────────────────────

def execute_tool(name, args):
    try:
        if name == "get_script_stats":
            by_type = NukeData.nodes_by_type()
            return json.dumps({"total": sum(by_type.values()), "by_type": by_type})

        if name == "get_nodes_of_type":
            names = NukeData.node_names_of_type(args["node_type"])
            return json.dumps({"count": len(names), "nodes": names, "type": args["node_type"]})

        if name == "get_missing_files":
            return json.dumps(NukeData.missing_files())

        if name == "get_error_nodes":
            return json.dumps(NukeData.error_nodes())

        if name == "get_disabled_nodes":
            return json.dumps(NukeData.disabled_nodes())

        if name == "get_script_info":
            return json.dumps(NukeData.script_info())

        if name == "get_node_knobs":
            r = NukeData.node_knobs(args["node_name"])
            return json.dumps(r or {"error": f"Node '{args['node_name']}' not found"})

        if name == "get_node_connections":
            r = NukeData.node_connections(args["node_name"])
            return json.dumps(r or {"error": f"Node '{args['node_name']}' not found"})

        if name == "get_heavy_nodes":
            return json.dumps(NukeData.heavy_nodes())

        if name == "create_node":
            if not NUKE_ENV: return json.dumps({"error":"Not in Nuke"})
            n = NukeActions.create_node(args["node_type"], args.get("name"),
                                        args.get("input_node"), args.get("knobs",{}))
            return json.dumps({"created": n})

        if name == "set_knob":
            if not NUKE_ENV: return json.dumps({"error":"Not in Nuke"})
            ok = NukeActions.set_knob(args["node_name"], args["knob"], args["value"])
            return json.dumps({"success": ok})

        if name == "batch_set":
            if not NUKE_ENV: return json.dumps({"error":"Not in Nuke"})
            changed = NukeActions.batch_set(args["node_type"], args["knobs"])
            return json.dumps({"modified_count": len(changed), "nodes": changed})

        if name == "delete_node":
            if not NUKE_ENV: return json.dumps({"error":"Not in Nuke"})
            return json.dumps({"success": NukeActions.delete_node(args["node_name"])})

        if name == "rename_node":
            if not NUKE_ENV: return json.dumps({"error":"Not in Nuke"})
            return json.dumps({"success": NukeActions.rename_node(args["old_name"], args["new_name"])})

        if name == "connect_nodes":
            if not NUKE_ENV: return json.dumps({"error":"Not in Nuke"})
            ok = NukeActions.connect_nodes(args["source"], args["destination"], args.get("input_index",0))
            return json.dumps({"success": ok})

        if name == "update_file_paths":
            if not NUKE_ENV: return json.dumps({"error":"Not in Nuke"})
            updated = NukeActions.update_paths(args["old_prefix"], args["new_prefix"])
            return json.dumps({"updated": updated})

        if name == "save_snapshot":
            if not NUKE_ENV: return json.dumps({"error":"Not in Nuke"})
            path = NukeActions.snapshot(args["label"])
            return json.dumps({"path": path})

        if name == "organize_by_type":
            if not NUKE_ENV: return json.dumps({"error":"Not in Nuke"})
            NukeActions.organize_by_type()
            return json.dumps({"success": True})

        if name == "create_smart_setup":
            if not NUKE_ENV: return json.dumps({"error":"Not in Nuke"})
            created = NukeActions.create_smart_setup(args["pattern"], args["source_node"])
            return json.dumps({"created": created})

        return json.dumps({"error": f"Unknown tool: {name}"})

    except Exception as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────
#  AI ENGINE  –  Text-based tool parsing (works with any model)
#
#  Instead of relying on Ollama's native tool-call protocol
#  (which qwen2.5-coder ignores), we inject the tool list as
#  plain text and parse TOOL_CALL: lines from the response.
#  This gives 100% reliable tool execution with any local model.
# ─────────────────────────────────────────────────────────────

def _build_system_prompt():
    """Build the full system prompt with tool list embedded as plain text."""

    tool_docs = []
    for t in TOOLS:
        props = t["input_schema"].get("properties", {})
        required = t["input_schema"].get("required", [])
        params = []
        for pname, pdef in props.items():
            req = " (required)" if pname in required else " (optional)"
            desc = pdef.get("description", "")
            params.append(f"    - {pname}{req}: {desc}")
        param_str = "\n".join(params) if params else "    (no parameters)"
        tool_docs.append(f"  {t['name']}: {t['description']}\n{param_str}")

    tools_text = "\n\n".join(tool_docs)

    return f"""You are a Nuke VFX assistant embedded inside Nuke. You help compositors query and modify their live Nuke script.

You have access to tools that interact with the live Nuke session. You MUST use them — do not pretend, guess, or explain what you would do. Actually do it.

═══════════════════════════════════════════════════════
HOW TO CALL A TOOL — follow this format EXACTLY:
═══════════════════════════════════════════════════════

TOOL_CALL: {{"tool": "tool_name", "args": {{...}}}}

Rules:
- Output TOOL_CALL on its own line with valid JSON after the colon
- After outputting TOOL_CALL, stop. Do not write anything else yet.
- Wait for the TOOL_RESULT to come back, then respond to the user.
- You may chain multiple tool calls one at a time if needed (e.g. create node, then connect it).
- For greetings/small talk only: reply normally, no tool call needed.
- NEVER explain what you are going to do. Just do it with TOOL_CALL.
- NEVER write JSON code blocks as a response to the user. Use TOOL_CALL instead.

═══════════════════════════════════════════════════════
TOOL SELECTION RULES:
═══════════════════════════════════════════════════════
- "how many X nodes" or "list all X nodes" → get_nodes_of_type with node_type=X
- "how many nodes total" or "script stats" → get_script_stats
- "what is the [knob] of [node]" or "what are the settings of [node]" → get_node_knobs
- "create a X node" → create_node, then connect_nodes if target mentioned
- "connect X to Y" → connect_nodes
- "disable/enable all X" → batch_set with knobs={{"disable": true/false}}
- "set [knob] on X to Y" → set_knob
- "delete X" → delete_node
- "rename X to Y" → rename_node
- "missing files" → get_missing_files
- "errors" → get_error_nodes
- "disabled nodes" → get_disabled_nodes
- "performance / heavy nodes" → get_heavy_nodes
- "organize nodes" → organize_by_type

═══════════════════════════════════════════════════════
AVAILABLE TOOLS:
═══════════════════════════════════════════════════════

{tools_text}

═══════════════════════════════════════════════════════
EXAMPLES:
═══════════════════════════════════════════════════════

User: how many blur nodes?
Assistant: TOOL_CALL: {{"tool": "get_nodes_of_type", "args": {{"node_type": "Blur"}}}}

User: [TOOL_RESULT: {{"count": 3, "nodes": ["Blur1","Blur2","Blur3"]}}]
Assistant: You have 3 Blur nodes: Blur1, Blur2, and Blur3.

---

User: add a sharpen node after Read1 and connect it to the viewer
Assistant: TOOL_CALL: {{"tool": "create_node", "args": {{"node_type": "Sharpen", "name": "Sharpen1", "input_node": "Read1"}}}}

User: [TOOL_RESULT: {{"created": "Sharpen1"}}]
Assistant: TOOL_CALL: {{"tool": "connect_nodes", "args": {{"source": "Sharpen1", "destination": "Viewer1"}}}}

User: [TOOL_RESULT: {{"success": true}}]
Assistant: Done! I created Sharpen1 after Read1 and connected it to Viewer1.

---

User: what is the size knob on Blur1?
Assistant: TOOL_CALL: {{"tool": "get_node_knobs", "args": {{"node_name": "Blur1"}}}}

User: [TOOL_RESULT: {{"size": 10.0, "channels": "rgba", ...}}]
Assistant: The size knob on Blur1 is set to 10.0.

---

User: disable all grade nodes
Assistant: TOOL_CALL: {{"tool": "batch_set", "args": {{"node_type": "Grade", "knobs": {{"disable": true}}}}}}

User: [TOOL_RESULT: {{"modified_count": 4, "nodes": ["Grade1","Grade2","Grade3","Grade4"]}}]
Assistant: Done! Disabled 4 Grade nodes: Grade1, Grade2, Grade3, and Grade4.

---

User: hi
Assistant: Hey! What can I help you with in your Nuke script?
"""


class NukeAI:
    """
    Agentic AI engine using text-based TOOL_CALL parsing.
    Works reliably with qwen2.5-coder and other local models
    that don't honour Ollama's native tool-call protocol.
    """

    # Matches:  TOOL_CALL: {...}
    _TOOL_RE = re.compile(r'TOOL_CALL:\s*(\{.*\})', re.DOTALL)

    def __init__(self):
        self.api_url = "http://localhost:11434/api/generate"
        self.model   = "qwen2.5-coder:7b"
        self.history = []          # list of {"role": "user"|"assistant", "content": str}
        self._sys    = _build_system_prompt()

    # ── public ───────────────────────────────────────────────

    def chat(self, user_message, log_cb=None):
        def log(msg):
            if log_cb: log_cb(msg)

        self.history.append({"role": "user", "content": user_message})

        for round_n in range(8):   # allow up to 8 tool calls per user message
            prompt = self._build_prompt()

            try:
                resp = requests.post(
                    self.api_url,
                    json={
                        "model":  self.model,
                        "prompt": prompt,
                        "stream": False,
                        "options": {
                            "temperature": 0.2,
                            "num_predict": 512,
                            "num_ctx":     6000,
                            "stop": ["[TOOL_RESULT", "User:", "\nUser:"],
                        },
                    },
                    timeout=90,
                )
                resp.raise_for_status()
                raw = resp.json().get("response", "").strip()
            except requests.exceptions.ConnectionError:
                reply = "Can't reach Ollama — make sure it's running: ollama serve"
                log("❌ Ollama not reachable")
                self.history.append({"role": "assistant", "content": reply})
                return reply
            except Exception as e:
                reply = f"Request error: {e}"
                log(f"❌ {e}")
                return reply

            log(f"🤖 raw[{round_n}]: {raw[:200]}{'…' if len(raw)>200 else ''}")

            # Check for TOOL_CALL in response
            match = self._TOOL_RE.search(raw)
            if not match:
                # No tool call → this is the final answer
                # Strip any leftover TOOL_CALL artifacts just in case
                final = re.sub(r'TOOL_CALL:.*', '', raw, flags=re.DOTALL).strip()
                self.history.append({"role": "assistant", "content": final})
                return final

            # Parse the tool call
            json_str = match.group(1)
            # Grab any text before the TOOL_CALL as partial assistant text
            pre_text = raw[:match.start()].strip()

            try:
                call     = json.loads(json_str)
                tname    = call.get("tool", "")
                targs    = call.get("args", {})
            except json.JSONDecodeError as e:
                log(f"❌ Bad JSON in TOOL_CALL: {e} — raw: {json_str[:120]}")
                reply = "I got confused trying to run that. Could you rephrase?"
                self.history.append({"role": "assistant", "content": reply})
                return reply

            log(f"🔧 Calling: {tname}({targs})")
            result_str = execute_tool(tname, targs)
            log(f"   ↳ {result_str[:150]}{'…' if len(result_str)>150 else ''}")

            # Append the assistant's TOOL_CALL turn + the result into history
            # so the model knows what happened
            turn = (pre_text + "\n" if pre_text else "") + f"TOOL_CALL: {json_str}"
            self.history.append({"role": "assistant", "content": turn})
            self.history.append({"role": "user",      "content": f"[TOOL_RESULT: {result_str}]"})

        return "I had trouble completing that in time. Please try again."

    def reset(self):
        self.history = []

    # ── private ──────────────────────────────────────────────

    def _build_prompt(self):
        """Render history as a single string prompt for /api/generate."""
        parts = [self._sys, "\n\n"]
        for msg in self.history:
            role = msg["role"]
            text = msg["content"]
            if role == "user":
                parts.append(f"User: {text}\n")
            else:
                parts.append(f"Assistant: {text}\n")
        parts.append("Assistant: ")
        return "".join(parts)


# ─────────────────────────────────────────────────────────────
#  GUI
# ─────────────────────────────────────────────────────────────

class _Worker(QtCore.QThread):
    log_sig  = QtCore.Signal(str)
    done_sig = QtCore.Signal(str)

    def __init__(self, ai, message):
        super().__init__()
        self.ai      = ai
        self.message = message

    def run(self):
        reply = self.ai.chat(self.message, log_cb=self.log_sig.emit)
        self.done_sig.emit(reply)


class NukeAIPanel(QtWidgets.QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ai = NukeAI()
        self.setWindowTitle("Nuke AI Assistant")
        self.setMinimumSize(700, 560)
        self._build_ui()

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0,0,0,0)
        root.setSpacing(0)

        # ── Header
        header = QtWidgets.QWidget()
        header.setFixedHeight(50)
        header.setStyleSheet("background:#0d0d0d;")
        hl = QtWidgets.QHBoxLayout(header)
        hl.setContentsMargins(18,0,18,0)

        title = QtWidgets.QLabel("◈  Nuke AI Assistant")
        title.setFont(QtGui.QFont("Courier New", 13, QtGui.QFont.Bold))
        title.setStyleSheet("color:#c8f046;letter-spacing:1px;background:transparent;")

        self.status = QtWidgets.QLabel("● ready")
        self.status.setFont(QtGui.QFont("Segoe UI", 8))
        self.status.setStyleSheet("color:#3c3;background:transparent;")

        new_btn = QtWidgets.QPushButton("New Chat")
        new_btn.setFixedHeight(26)
        new_btn.setStyleSheet("""
            QPushButton{background:#1e1e1e;color:#666;border:1px solid #2e2e2e;
                        border-radius:4px;padding:0 12px;font-size:10px;}
            QPushButton:hover{color:#bbb;border-color:#555;}
        """)
        new_btn.clicked.connect(lambda _: self._new_chat())

        hl.addWidget(title)
        hl.addSpacing(10)
        hl.addWidget(self.status)
        hl.addStretch()
        hl.addWidget(new_btn)
        root.addWidget(header)

        # ── Chat scroll area
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("""
            QScrollArea{background:#0a0a0a;border:none;}
            QScrollBar:vertical{background:#111;width:5px;border-radius:3px;}
            QScrollBar::handle:vertical{background:#2e2e2e;border-radius:3px;}
            QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}
        """)
        self._msg_container = QtWidgets.QWidget()
        self._msg_container.setStyleSheet("background:#0a0a0a;")
        self._msg_layout = QtWidgets.QVBoxLayout(self._msg_container)
        self._msg_layout.setContentsMargins(16,16,16,16)
        self._msg_layout.setSpacing(10)
        self._msg_layout.addStretch()
        self.scroll.setWidget(self._msg_container)
        root.addWidget(self.scroll, stretch=1)

        # ── Tool log toggle
        self._log_btn = QtWidgets.QPushButton("▸ Tool log")
        self._log_btn.setCheckable(True)
        self._log_btn.setFixedHeight(24)
        self._log_btn.setStyleSheet("""
            QPushButton{background:#0d0d0d;color:#444;border:none;
                        border-top:1px solid #1e1e1e;text-align:left;
                        padding:0 14px;font-size:10px;font-family:'Courier New';}
            QPushButton:checked{color:#777;}
        """)
        self._log_btn.clicked.connect(lambda _: self._toggle_log())
        root.addWidget(self._log_btn)

        self._log_box = QtWidgets.QTextEdit()
        self._log_box.setReadOnly(True)
        self._log_box.setMaximumHeight(110)
        self._log_box.setFont(QtGui.QFont("Courier New", 8))
        self._log_box.setStyleSheet("""
            QTextEdit{background:#050505;color:#444;border:none;padding:6px 14px;}
        """)
        self._log_box.hide()
        root.addWidget(self._log_box)

        # ── Quick pills
        pills_wrap = QtWidgets.QWidget()
        pills_wrap.setFixedHeight(36)
        pills_wrap.setStyleSheet("background:#0d0d0d;border-top:1px solid #1a1a1a;")
        pw = QtWidgets.QHBoxLayout(pills_wrap)
        pw.setContentsMargins(12,0,12,0)
        pw.setSpacing(6)

        ql = QtWidgets.QLabel("Try:")
        ql.setStyleSheet("color:#333;font-size:9px;background:transparent;")
        ql.setFont(QtGui.QFont("Segoe UI",9))
        pw.addWidget(ql)

        for label, prompt in [
            ("node count",    "How many nodes do I have in total?"),
            ("read nodes",    "How many Read nodes are in the script?"),
            ("missing files", "Are there any missing files?"),
            ("errors",        "Which nodes have errors?"),
            ("performance",   "What are the heaviest nodes in the script?"),
        ]:
            p = QtWidgets.QPushButton(label)
            p.setFixedHeight(22)
            p.setStyleSheet("""
                QPushButton{background:#161616;color:#555;border:1px solid #252525;
                            border-radius:11px;padding:0 10px;font-size:9px;}
                QPushButton:hover{color:#c8f046;border-color:#c8f046;}
            """)
            p.clicked.connect(lambda _=False, pr=prompt: self._send(pr))
            pw.addWidget(p)
        pw.addStretch()
        root.addWidget(pills_wrap)

        # ── Input bar
        bar = QtWidgets.QWidget()
        bar.setFixedHeight(56)
        bar.setStyleSheet("background:#0d0d0d;border-top:1px solid #1a1a1a;")
        bl = QtWidgets.QHBoxLayout(bar)
        bl.setContentsMargins(14,10,14,10)
        bl.setSpacing(8)

        self.inp = QtWidgets.QLineEdit()
        self.inp.setPlaceholderText("Ask anything about your Nuke script…")
        self.inp.setFont(QtGui.QFont("Segoe UI",10))
        self.inp.setFixedHeight(34)
        self.inp.setStyleSheet("""
            QLineEdit{background:#181818;color:#e0e0e0;
                      border:1px solid #2c2c2c;border-radius:17px;padding:0 15px;}
            QLineEdit:focus{border-color:#c8f046;}
        """)
        self.inp.returnPressed.connect(self._on_enter)

        self.send = QtWidgets.QPushButton("↑")
        self.send.setFixedSize(34,34)
        self.send.setFont(QtGui.QFont("Arial",13,QtGui.QFont.Bold))
        self.send.setStyleSheet("""
            QPushButton{background:#c8f046;color:#111;border:none;border-radius:17px;}
            QPushButton:hover{background:#d8ff56;}
            QPushButton:disabled{background:#222;color:#444;}
        """)
        self.send.clicked.connect(lambda _: self._on_enter())

        bl.addWidget(self.inp, stretch=1)
        bl.addWidget(self.send)
        root.addWidget(bar)

        self.setStyleSheet("QWidget{background:#0a0a0a;color:#ccc;}")

        # Welcome message
        self._add_ai_bubble(
            "Hey! I'm your Nuke AI assistant 👋\n\n"
            "Ask me anything about your script — like:\n"
            "• \"How many Read nodes do I have?\"\n"
            "• \"Which nodes have errors?\"\n"
            "• \"Create a Grade node after Read1\"\n"
            "• \"Disable all Blur nodes\"\n\n"
            "What do you need?"
        )

    # ── Bubbles ──────────────────────────────────────────────

    def _add_user_bubble(self, text):
        row = QtWidgets.QHBoxLayout()
        row.addStretch()
        lbl = self._bubble(text, bg="#c8f046", fg="#111",
                           br="16px 16px 4px 16px")
        row.addWidget(lbl)
        self._push(row)

    def _add_ai_bubble(self, text):
        row = QtWidgets.QHBoxLayout()
        icon = QtWidgets.QLabel("◈")
        icon.setFixedWidth(24)
        icon.setAlignment(QtCore.Qt.AlignTop)
        icon.setFont(QtGui.QFont("Arial",14))
        icon.setStyleSheet("color:#c8f046;padding-top:5px;background:transparent;")
        lbl = self._bubble(text, bg="#181818", fg="#d8d8d8",
                           br="16px 16px 16px 4px",
                           border="1px solid #2a2a2a")
        row.addWidget(icon)
        row.addWidget(lbl)
        row.addStretch()
        self._push(row)

    def _add_typing(self):
        row = QtWidgets.QHBoxLayout()
        icon = QtWidgets.QLabel("◈")
        icon.setFixedWidth(24)
        icon.setAlignment(QtCore.Qt.AlignTop)
        icon.setFont(QtGui.QFont("Arial",14))
        icon.setStyleSheet("color:#c8f046;padding-top:5px;background:transparent;")
        lbl = self._bubble("thinking…", bg="#111", fg="#444",
                           br="16px 16px 16px 4px",
                           border="1px solid #222")
        lbl.setFont(QtGui.QFont("Courier New",9))
        row.addWidget(icon)
        row.addWidget(lbl)
        row.addStretch()
        self._typing_lbl = lbl
        self._typing_row = row
        self._push(row)

    def _remove_typing(self):
        if hasattr(self, "_typing_lbl"):
            self._typing_lbl.hide()

    def _bubble(self, text, bg, fg, br, border="none"):
        lbl = QtWidgets.QLabel(text)
        lbl.setWordWrap(True)
        lbl.setMaximumWidth(480)
        lbl.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        lbl.setFont(QtGui.QFont("Segoe UI",10))
        lbl.setStyleSheet(f"""
            background:{bg};color:{fg};border-radius:{br};
            border:{border};padding:9px 13px;
        """)
        return lbl

    def _push(self, layout):
        count = self._msg_layout.count()
        self._msg_layout.insertLayout(count-1, layout)
        QtCore.QTimer.singleShot(30, lambda: self.scroll.verticalScrollBar().setValue(
            self.scroll.verticalScrollBar().maximum()))

    # ── Send / receive ────────────────────────────────────────

    def _on_enter(self):
        text = self.inp.text().strip()
        if text:
            self._send(text)

    def _send(self, text):
        self.inp.clear()
        self.inp.setEnabled(False)
        self.send.setEnabled(False)
        self.status.setText("● thinking…")
        self.status.setStyleSheet("color:#fa0;background:transparent;font-size:8px;")

        self._add_user_bubble(text)
        self._add_typing()

        self._worker = _Worker(self.ai, text)
        self._worker.log_sig.connect(self._on_log)
        self._worker.done_sig.connect(self._on_reply)
        self._worker.start()

    @QtCore.Slot(str)
    def _on_log(self, msg):
        self._log_box.append(msg)

    @QtCore.Slot(str)
    def _on_reply(self, reply):
        self._remove_typing()
        self._add_ai_bubble(reply)
        self.inp.setEnabled(True)
        self.send.setEnabled(True)
        self.inp.setFocus()
        self.status.setText("● ready")
        self.status.setStyleSheet("color:#3c3;background:transparent;font-size:8px;")

    def _toggle_log(self):
        on = self._log_btn.isChecked()
        self._log_box.setVisible(on)
        self._log_btn.setText("▾ Tool log" if on else "▸ Tool log")

    def _new_chat(self):
        self.ai.reset()
        while self._msg_layout.count() > 1:
            item = self._msg_layout.takeAt(0)
            if item.layout():
                while item.layout().count():
                    w = item.layout().takeAt(0).widget()
                    if w: w.deleteLater()
        self._log_box.clear()
        self._add_ai_bubble("Fresh start! What can I help you with?")


# ─────────────────────────────────────────────────────────────
#  LAUNCH
# ─────────────────────────────────────────────────────────────

_panel = None

def show_nuke_ai_panel():
    global _panel
    if _panel and _panel.isVisible():
        _panel.raise_()
        _panel.activateWindow()
        return _panel
    _panel = NukeAIPanel()
    _panel.show()
    return _panel

def add_to_nuke_menu():
    m = nuke.menu("Nuke").addMenu("AI Assistant")
    m.addCommand("Open Panel", show_nuke_ai_panel, "ctrl+shift+a")

# Auto-launch when run from Script Editor
show_nuke_ai_panel()
