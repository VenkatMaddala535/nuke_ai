# =============================================================================
#  NUKE AI ASSISTANT
#  Save this file anywhere, then in Nuke's Script Editor run:
#
#      import sys
#      sys.path.insert(0, "/path/to/folder/containing/this/file")
#      import nuke_ai_assistant
#      nuke_ai_assistant.show()
#
#  Or add those lines to ~/.nuke/menu.py for a permanent menu entry.
#  Requires: Ollama running locally  →  ollama serve
# =============================================================================

import json, os, re, requests
from datetime import datetime

try:
    import nuke, nukescripts
    from PySide2 import QtWidgets, QtCore, QtGui
    NUKE_ENV = True
except ImportError:
    NUKE_ENV = False


# ─────────────────────────────────────────────────────────────────────────────
#  KNOB ALIAS MAP
#  Maps human words → actual Nuke knob names so the LLM never has to guess.
# ─────────────────────────────────────────────────────────────────────────────

KNOB_ALIASES = {
    # Blur
    "percentage": "size", "amount": "size", "radius": "size", "strength": "size",
    "blur":       "size",
    # Grade
    "gain": "white", "lift": "black", "gamma": "gamma", "multiply": "multiply",
    # Sharpen
    "sharpness": "size",
    # Transform
    "rotation": "rotate", "position": "translate", "scale_x": "scale",
    # Common
    "opacity": "mix", "alpha": "mix", "blend": "mix",
    "on":  "disable", "off": "disable", "enabled": "disable",
    "width": "size", "height": "size",
}

def resolve_knob(node, knob_name):
    """
    Given a human knob name, return the real knob name that exists on this node.
    Checks: exact match → alias map → fuzzy substring match.
    Returns (real_name, found: bool).
    """
    knobs = node.knobs()

    # Exact match
    if knob_name in knobs:
        return knob_name, True

    # Alias map
    alias = KNOB_ALIASES.get(knob_name.lower())
    if alias and alias in knobs:
        return alias, True

    # Fuzzy: find knobs whose name contains the word
    low = knob_name.lower()
    candidates = [k for k in knobs if low in k.lower()]
    if len(candidates) == 1:
        return candidates[0], True

    return knob_name, False


# ─────────────────────────────────────────────────────────────────────────────
#  NUKE DATA LAYER
# ─────────────────────────────────────────────────────────────────────────────

class NukeData:

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
    def find_node(name):
        """Find node by exact name or case-insensitive match."""
        if not NUKE_ENV:
            return None
        node = nuke.toNode(name)
        if node:
            return node
        # Case-insensitive fallback
        low = name.lower()
        for n in nuke.allNodes():
            if n.name().lower() == low:
                return n
        return None

    @staticmethod
    def get_knob_value(node_name, knob_name):
        """
        Get a knob value with alias resolution.
        Returns dict with keys: value, real_knob, found, available_knobs (on error).
        """
        node = NukeData.find_node(node_name)
        if not node:
            all_names = [n.name() for n in NukeData.all_nodes()]
            return {
                "error": f"Node '{node_name}' not found",
                "all_nodes": all_names
            }

        real_knob, found = resolve_knob(node, knob_name)
        if not found:
            # Return available knobs to help the LLM recover
            readable = {}
            for k in node.allKnobs():
                try:
                    v = k.value()
                    if isinstance(v, (int, float, str, bool)):
                        readable[k.name()] = v
                except Exception:
                    pass
            return {
                "error": f"Knob '{knob_name}' not found on {node_name}",
                "hint": f"Available knobs with simple values: {readable}"
            }

        try:
            v = node[real_knob].value()
            return {"node": node_name, "knob": real_knob, "value": v}
        except Exception as e:
            return {"error": str(e)}

    @staticmethod
    def get_all_knobs(node_name):
        node = NukeData.find_node(node_name)
        if not node:
            return {"error": f"Node '{node_name}' not found",
                    "all_nodes": [n.name() for n in NukeData.all_nodes()]}
        out = {}
        for k in node.allKnobs():
            try:
                v = k.value()
                if isinstance(v, (int, float, str, bool)):
                    out[k.name()] = v
            except Exception:
                pass
        return {"node": node_name, "knobs": out}

    @staticmethod
    def get_frame_range(node_name):
        node = NukeData.find_node(node_name)
        if not node:
            return {"error": f"Node '{node_name}' not found"}
        result = {"node": node_name, "type": node.Class()}
        for k in ("first", "last", "first_frame", "last_frame", "origfirst", "origlast"):
            if k in node.knobs():
                try:
                    result[k] = int(node[k].value())
                except Exception:
                    pass
        if len(result) <= 2:
            return {"error": f"{node_name} has no frame range knobs", "type": node.Class()}
        return result

    @staticmethod
    def get_connections(node_name):
        node = NukeData.find_node(node_name)
        if not node:
            return {"error": f"Node '{node_name}' not found"}
        inputs  = [node.input(i).name() if node.input(i) else None for i in range(node.inputs())]
        outputs = [d.name() for d in node.dependent()]
        return {"node": node_name, "inputs": inputs, "outputs": outputs}

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
        return {
            "path":        nuke.root().name(),
            "fps":         nuke.root()["fps"].value(),
            "first_frame": int(nuke.root()["first_frame"].value()),
            "last_frame":  int(nuke.root()["last_frame"].value()),
        }

    @staticmethod
    def heavy_nodes():
        heavy = []
        for n in NukeData.all_nodes():
            if n.Class() == "Blur" and "size" in n.knobs():
                if n["size"].value() > 50:
                    heavy.append({"node": n.name(), "reason": f"Blur size={n['size'].value()}"})
            if n.Class() in ("Denoise","Defocus","ZDefocus","VectorBlur",
                             "MotionBlur2D","Kronos","DeepToImage"):
                heavy.append({"node": n.name(), "reason": f"Heavy op: {n.Class()}"})
        return heavy


# ─────────────────────────────────────────────────────────────────────────────
#  NUKE ACTION LAYER
# ─────────────────────────────────────────────────────────────────────────────

class NukeActions:

    @staticmethod
    def create_node(node_type, name=None, input_node=None, knobs=None):
        aliases = {"Backdrop":"BackdropNode","Merge":"Merge2","Shuffle":"Shuffle2"}
        cls  = aliases.get(node_type, node_type)
        try:
            node = getattr(nuke.nodes, cls)()
        except AttributeError:
            return {"error": f"Unknown node type: {node_type}"}
        if name:
            node["name"].setValue(name)
        if input_node:
            src = NukeData.find_node(input_node)
            if src:
                node.setInput(0, src)
            else:
                # Don't fail — just warn
                return {"created": node.name(),
                        "warning": f"Could not find input node '{input_node}' — node created unconnected"}
        errors = []
        for k, v in (knobs or {}).items():
            real_k, found = resolve_knob(node, k)
            if found:
                try:
                    node[real_k].setValue(v)
                except Exception as e:
                    errors.append(f"{real_k}: {e}")
            else:
                errors.append(f"knob '{k}' not found")
        result = {"created": node.name()}
        if errors:
            result["knob_warnings"] = errors
        return result

    @staticmethod
    def set_knob(node_name, knob_name, value):
        node = NukeData.find_node(node_name)
        if not node:
            all_names = [n.name() for n in NukeData.all_nodes()]
            return {"success": False,
                    "error": f"Node '{node_name}' not found",
                    "all_nodes": all_names}

        real_knob, found = resolve_knob(node, knob_name)
        if not found:
            readable = {k: None for k in list(node.knobs().keys())[:30]}
            return {"success": False,
                    "error": f"Knob '{knob_name}' not found on {node_name}",
                    "available_knobs": list(node.knobs().keys())}

        try:
            node[real_knob].setValue(value)
            new_val = node[real_knob].value()
            return {"success": True, "node": node_name,
                    "knob": real_knob, "new_value": new_val}
        except Exception as e:
            return {"success": False, "error": str(e),
                    "knob": real_knob, "tried_value": value}

    @staticmethod
    def batch_set(node_type, knob_name, value):
        nodes = NukeData.nodes_of_type(node_type)
        if not nodes:
            return {"error": f"No nodes of type '{node_type}' found",
                    "available_types": list(NukeData.nodes_by_type().keys())}
        changed, failed = [], []
        for n in nodes:
            real_k, found = resolve_knob(n, knob_name)
            if found:
                try:
                    n[real_k].setValue(value)
                    changed.append(n.name())
                except Exception as e:
                    failed.append({"node": n.name(), "error": str(e)})
            else:
                failed.append({"node": n.name(), "error": f"knob '{knob_name}' not found"})
        return {"modified": changed, "failed": failed, "count": len(changed)}

    @staticmethod
    def connect_nodes(src_name, dst_name, input_index=0):
        src = NukeData.find_node(src_name)
        dst = NukeData.find_node(dst_name)
        errors = []
        if not src:
            errors.append(f"Source '{src_name}' not found")
        if not dst:
            errors.append(f"Destination '{dst_name}' not found")
        if errors:
            all_names = [n.name() for n in NukeData.all_nodes()]
            return {"success": False, "errors": errors, "all_nodes": all_names}
        try:
            dst.setInput(input_index, src)
            return {"success": True,
                    "connected": f"{src.name()} → {dst.name()} (input {input_index})"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @staticmethod
    def delete_node(name):
        node = NukeData.find_node(name)
        if not node:
            return {"success": False, "error": f"Node '{name}' not found"}
        nuke.delete(node)
        return {"success": True, "deleted": name}

    @staticmethod
    def rename_node(old_name, new_name):
        node = NukeData.find_node(old_name)
        if not node:
            return {"success": False, "error": f"Node '{old_name}' not found"}
        node["name"].setValue(new_name)
        return {"success": True, "new_name": node.name()}

    @staticmethod
    def update_paths(old_prefix, new_prefix):
        updated = []
        for n in NukeData.all_nodes():
            if "file" in n.knobs():
                p = n["file"].value()
                if p and p.startswith(old_prefix):
                    n["file"].setValue(p.replace(old_prefix, new_prefix, 1))
                    updated.append(n.name())
        return {"updated": updated, "count": len(updated)}

    @staticmethod
    def snapshot(label):
        script_path = nuke.root().name()
        if not script_path or script_path in ("Root", ""):
            return {"error": "Script not saved yet — save first"}
        snap_dir = os.path.join(os.path.dirname(script_path), "snapshots")
        os.makedirs(snap_dir, exist_ok=True)
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(snap_dir, f"{label}_{ts}.nk")
        nuke.scriptSave(path)
        return {"saved": path}

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
        return {"success": True}

    @staticmethod
    def create_smart_setup(pattern, source_name):
        src = NukeData.find_node(source_name)
        if not src:
            return {"error": f"Source node '{source_name}' not found"}
        chains = {
            "beauty_comp":    [("Grade","Grade",{}),("Blur","Blur",{"size":1}),("Sharpen","Sharpen",{})],
            "color_correct":  [("ColorCorrect","CC",{}),("Saturation","Sat",{}),("HueCorrect","Hue",{})],
            "denoise_sharpen":[("Denoise","Denoise",{}),("Sharpen","Sharpen",{})],
        }
        specs = chains.get(pattern)
        if not specs:
            return {"error": f"Unknown pattern '{pattern}'",
                    "available": list(chains.keys())}
        prev, created = src, []
        for cls, suffix, knobs in specs:
            r = NukeActions.create_node(cls, f"{source_name}_{suffix}", prev.name(), knobs)
            created.append(r.get("created", "?"))
            prev = NukeData.find_node(created[-1]) or prev
        return {"created": created}


# ─────────────────────────────────────────────────────────────────────────────
#  TOOL EXECUTOR
# ─────────────────────────────────────────────────────────────────────────────

def execute_tool(name, args):
    """Run a tool and return a JSON string result."""
    try:
        # ── Queries ────────────────────────────────────────────
        if name == "get_script_stats":
            by_type = NukeData.nodes_by_type()
            return json.dumps({"total": sum(by_type.values()), "by_type": by_type})

        if name == "get_nodes_of_type":
            t     = args.get("node_type", "")
            names = NukeData.node_names_of_type(t)
            return json.dumps({"count": len(names), "nodes": names, "type": t})

        if name == "get_knob_value":
            return json.dumps(NukeData.get_knob_value(args["node_name"], args["knob_name"]))

        if name == "get_all_knobs":
            return json.dumps(NukeData.get_all_knobs(args["node_name"]))

        if name == "get_frame_range":
            return json.dumps(NukeData.get_frame_range(args["node_name"]))

        if name == "get_connections":
            return json.dumps(NukeData.get_connections(args["node_name"]))

        if name == "get_missing_files":
            return json.dumps(NukeData.missing_files())

        if name == "get_error_nodes":
            return json.dumps(NukeData.error_nodes())

        if name == "get_disabled_nodes":
            return json.dumps(NukeData.disabled_nodes())

        if name == "get_script_info":
            return json.dumps(NukeData.script_info())

        if name == "get_heavy_nodes":
            return json.dumps(NukeData.heavy_nodes())

        if name == "list_all_nodes":
            nodes = [{"name": n.name(), "type": n.Class()} for n in NukeData.all_nodes()]
            return json.dumps({"nodes": nodes, "count": len(nodes)})

        # ── Actions ────────────────────────────────────────────
        if not NUKE_ENV:
            return json.dumps({"error": "Not running inside Nuke"})

        if name == "create_node":
            return json.dumps(NukeActions.create_node(
                args["node_type"], args.get("name"),
                args.get("input_node"), args.get("knobs", {})))

        if name == "set_knob":
            return json.dumps(NukeActions.set_knob(
                args["node_name"], args["knob_name"], args["value"]))

        if name == "batch_set":
            return json.dumps(NukeActions.batch_set(
                args["node_type"], args["knob_name"], args["value"]))

        if name == "connect_nodes":
            return json.dumps(NukeActions.connect_nodes(
                args["source"], args["destination"], args.get("input_index", 0)))

        if name == "delete_node":
            return json.dumps(NukeActions.delete_node(args["node_name"]))

        if name == "rename_node":
            return json.dumps(NukeActions.rename_node(args["old_name"], args["new_name"]))

        if name == "update_file_paths":
            return json.dumps(NukeActions.update_paths(args["old_prefix"], args["new_prefix"]))

        if name == "save_snapshot":
            return json.dumps(NukeActions.snapshot(args["label"]))

        if name == "organize_by_type":
            return json.dumps(NukeActions.organize_by_type())

        if name == "create_smart_setup":
            return json.dumps(NukeActions.create_smart_setup(
                args["pattern"], args["source_node"]))

        return json.dumps({"error": f"Unknown tool: {name}"})

    except KeyError as e:
        return json.dumps({"error": f"Missing argument: {e}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
#  SYSTEM PROMPT  (embedded tool list, examples, knob guidance)
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = r"""
You are a Nuke VFX assistant embedded inside a live Nuke session.
You MUST use tools to answer questions or take actions. Never guess — call a tool.

════════════════════════════════════════════
 HOW TO CALL A TOOL
════════════════════════════════════════════
Write exactly this on its own line and then STOP:

TOOL_CALL: {"tool": "tool_name", "args": {...}}

After the TOOL_RESULT comes back, continue.
You may chain multiple tool calls one at a time.
For pure greetings (hi, thanks) — reply without a tool call.
NEVER write code blocks or JSON as a reply. Always use TOOL_CALL.

════════════════════════════════════════════
 IMPORTANT KNOB RULES
════════════════════════════════════════════
- Nuke knob names are NOT the same as human words.
- "percentage", "amount", "radius", "strength" → use knob_name="size" for Blur/Sharpen.
- "gain" → "white", "lift" → "black" for Grade.
- "opacity" → "mix".
- If you are not 100% sure of a knob name → call get_all_knobs first, find the right name, THEN call set_knob.
- For "what is the blur percentage/amount/size" → call get_knob_value with knob_name="size".

════════════════════════════════════════════
 TOOL SELECTION RULES
════════════════════════════════════════════
"how many X nodes"          → get_nodes_of_type
"how many nodes total"      → get_script_stats
"list/name all nodes"       → list_all_nodes
"what is [knob] on [node]"  → get_knob_value  (NOT get_all_knobs)
"show all knobs of [node]"  → get_all_knobs
"frame range of [node]"     → get_frame_range
"connections of [node]"     → get_connections
"change [knob] on [node]"   → set_knob  (resolve knob name first if unsure)
"disable/enable all X"      → batch_set with knob_name="disable" value=true/false
"create X [after Y]"        → create_node  then connect_nodes if needed
"connect X to Y"            → connect_nodes
"delete X"                  → delete_node
"rename X to Y"             → rename_node
"missing files"             → get_missing_files
"errors"                    → get_error_nodes
"disabled nodes"            → get_disabled_nodes
"heavy / slow nodes"        → get_heavy_nodes
"organize nodes"            → organize_by_type
"script info / frame range" → get_script_info

════════════════════════════════════════════
 AVAILABLE TOOLS
════════════════════════════════════════════

get_script_stats          → no args
get_nodes_of_type         → node_type (str, e.g. "Blur")
list_all_nodes            → no args
get_knob_value            → node_name, knob_name
get_all_knobs             → node_name
get_frame_range           → node_name
get_connections           → node_name
get_missing_files         → no args
get_error_nodes           → no args
get_disabled_nodes        → no args
get_script_info           → no args
get_heavy_nodes           → no args

create_node               → node_type, name(opt), input_node(opt), knobs(opt dict)
set_knob                  → node_name, knob_name, value
batch_set                 → node_type, knob_name, value
connect_nodes             → source, destination, input_index(opt, default 0)
delete_node               → node_name
rename_node               → old_name, new_name
update_file_paths         → old_prefix, new_prefix
save_snapshot             → label
organize_by_type          → no args
create_smart_setup        → pattern ("beauty_comp"|"color_correct"|"denoise_sharpen"), source_node

════════════════════════════════════════════
 EXAMPLES
════════════════════════════════════════════

User: how many blur nodes?
TOOL_CALL: {"tool": "get_nodes_of_type", "args": {"node_type": "Blur"}}
[TOOL_RESULT: {"count": 2, "nodes": ["Blur1","Blur3"]}]
You have 2 Blur nodes: Blur1 and Blur3.

---
User: what is the blur percentage on Blur1?
TOOL_CALL: {"tool": "get_knob_value", "args": {"node_name": "Blur1", "knob_name": "size"}}
[TOOL_RESULT: {"node": "Blur1", "knob": "size", "value": 10.0}]
The blur size on Blur1 is 10.0.

---
User: change blur1 percentage to 90
TOOL_CALL: {"tool": "set_knob", "args": {"node_name": "Blur1", "knob_name": "size", "value": 90}}
[TOOL_RESULT: {"success": true, "node": "Blur1", "knob": "size", "new_value": 90.0}]
Done! Blur1's size is now 90.

---
User: what is the frame range of Read8?
TOOL_CALL: {"tool": "get_frame_range", "args": {"node_name": "Read8"}}
[TOOL_RESULT: {"node": "Read8", "first": 1001, "last": 1100}]
Read8 runs from frame 1001 to 1100 (100 frames).

---
User: create a new blur node with size 50 and attach to Read8
TOOL_CALL: {"tool": "create_node", "args": {"node_type": "Blur", "input_node": "Read8", "knobs": {"size": 50}}}
[TOOL_RESULT: {"created": "Blur5"}]
TOOL_CALL: {"tool": "get_nodes_of_type", "args": {"node_type": "Viewer"}}
[TOOL_RESULT: {"count": 1, "nodes": ["Viewer1"]}]
Created Blur5 (size=50) connected after Read8. Want me to also connect it to the Viewer?

---
User: add a sharpen node to Read8 and connect it to the viewer
TOOL_CALL: {"tool": "create_node", "args": {"node_type": "Sharpen", "input_node": "Read8"}}
[TOOL_RESULT: {"created": "Sharpen2"}]
TOOL_CALL: {"tool": "get_nodes_of_type", "args": {"node_type": "Viewer"}}
[TOOL_RESULT: {"count": 1, "nodes": ["Viewer1"]}]
TOOL_CALL: {"tool": "connect_nodes", "args": {"source": "Sharpen2", "destination": "Viewer1"}}
[TOOL_RESULT: {"success": true}]
Done! Created Sharpen2 after Read8 and connected it to Viewer1.

---
User: disable all grade nodes
TOOL_CALL: {"tool": "batch_set", "args": {"node_type": "Grade", "knob_name": "disable", "value": true}}
[TOOL_RESULT: {"modified": ["Grade1","Grade2"], "count": 2}]
Done! Disabled 2 Grade nodes: Grade1 and Grade2.

---
User: hi
Hey! What can I help you with in your Nuke script?
"""


# ─────────────────────────────────────────────────────────────────────────────
#  AI ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class NukeAI:
    """Agentic loop: parse TOOL_CALL lines, execute, feed results back."""

    _TOOL_RE = re.compile(r'TOOL_CALL:\s*(\{.+?\})\s*$', re.MULTILINE)

    def __init__(self):
        self.api_url = "http://localhost:11434/api/generate"
        self.model   = "qwen2.5-coder:7b"
        self.history = []   # [{"role": "user"|"assistant", "content": str}]

    def chat(self, user_message, log_cb=None):
        def log(msg):
            if log_cb:
                log_cb(msg)

        self.history.append({"role": "user", "content": user_message})

        for round_n in range(10):
            prompt = self._build_prompt()

            try:
                resp = requests.post(
                    self.api_url,
                    json={
                        "model":  self.model,
                        "prompt": prompt,
                        "stream": False,
                        "options": {
                            "temperature": 0.15,
                            "num_predict": 400,
                            "num_ctx":     8192,
                            "stop": ["[TOOL_RESULT", "\nUser:"],
                        },
                    },
                    timeout=120,
                )
                resp.raise_for_status()
                raw = resp.json().get("response", "").strip()
            except requests.exceptions.ConnectionError:
                reply = "Can't reach Ollama. Run: ollama serve"
                log("❌ Ollama not reachable")
                self.history.append({"role": "assistant", "content": reply})
                return reply
            except Exception as e:
                reply = f"Request error: {e}"
                log(f"❌ {e}")
                return reply

            log(f"🤖 [{round_n}] {raw[:300]}{'…' if len(raw)>300 else ''}")

            # Find first TOOL_CALL in the response
            match = self._TOOL_RE.search(raw)

            if not match:
                # Final answer — clean up any stray artifacts
                final = raw.replace("TOOL_CALL:", "").strip()
                self.history.append({"role": "assistant", "content": final})
                return final

            # Parse tool call
            json_str = match.group(1)
            pre_text = raw[:match.start()].strip()

            try:
                call  = json.loads(json_str)
                tname = call.get("tool", "")
                targs = call.get("args", {})
            except json.JSONDecodeError as e:
                log(f"❌ JSON parse error: {e}  raw={json_str[:100]}")
                # Feed error back so model can retry with correct JSON
                self.history.append({"role": "assistant", "content": raw})
                self.history.append({"role": "user",
                                     "content": f"[TOOL_RESULT: {{\"error\": \"Invalid JSON in TOOL_CALL: {e}\"}}]"})
                continue

            log(f"🔧 {tname}({json.dumps(targs)})")
            result_str = execute_tool(tname, targs)
            log(f"   ↳ {result_str[:200]}{'…' if len(result_str)>200 else ''}")

            # Save assistant turn (what it said before the tool call)
            asst_text = (pre_text + "\n" if pre_text else "") + f"TOOL_CALL: {json_str}"
            self.history.append({"role": "assistant", "content": asst_text})
            self.history.append({"role": "user",
                                 "content": f"[TOOL_RESULT: {result_str}]"})

        return "I had trouble completing that. Please try rephrasing."

    def reset(self):
        self.history = []

    def _build_prompt(self):
        parts = [SYSTEM_PROMPT.strip(), "\n\n"]
        for msg in self.history:
            role = "User" if msg["role"] == "user" else "Assistant"
            parts.append(f"{role}: {msg['content']}\n")
        parts.append("Assistant: ")
        return "".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
#  GUI  –  Proper Nuke Panel + floating window
# ─────────────────────────────────────────────────────────────────────────────

class _Worker(QtCore.QThread):
    log_sig  = QtCore.Signal(str)
    done_sig = QtCore.Signal(str)

    def __init__(self, ai, message):
        super().__init__()
        self.ai = ai
        self.message = message

    def run(self):
        reply = self.ai.chat(self.message, log_cb=self.log_sig.emit)
        self.done_sig.emit(reply)


class NukeAIWidget(QtWidgets.QWidget):
    """The actual chat widget — used both standalone and inside a Nuke panel."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ai = NukeAI()
        self._build_ui()

    # ── UI ───────────────────────────────────────────────────

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        hdr = QtWidgets.QWidget()
        hdr.setFixedHeight(46)
        hdr.setStyleSheet("background:#0d0d0d;")
        hl = QtWidgets.QHBoxLayout(hdr)
        hl.setContentsMargins(14, 0, 14, 0)

        title = QtWidgets.QLabel("◈  Nuke AI")
        title.setFont(QtGui.QFont("Courier New", 12, QtGui.QFont.Bold))
        title.setStyleSheet("color:#c8f046;background:transparent;letter-spacing:1px;")

        self._dot = QtWidgets.QLabel("●  ready")
        self._dot.setFont(QtGui.QFont("Segoe UI", 8))
        self._dot.setStyleSheet("color:#3c3;background:transparent;")

        new_btn = QtWidgets.QPushButton("New Chat")
        new_btn.setFixedHeight(24)
        new_btn.setStyleSheet("""
            QPushButton{background:#1a1a1a;color:#666;border:1px solid #2a2a2a;
                        border-radius:3px;padding:0 10px;font-size:9px;}
            QPushButton:hover{color:#bbb;border-color:#555;}
        """)
        new_btn.clicked.connect(lambda _: self._new_chat())

        hl.addWidget(title)
        hl.addSpacing(8)
        hl.addWidget(self._dot)
        hl.addStretch()
        hl.addWidget(new_btn)
        root.addWidget(hdr)

        # Chat scroll area
        self._scroll = QtWidgets.QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet("""
            QScrollArea{background:#0a0a0a;border:none;}
            QScrollBar:vertical{background:#111;width:5px;border-radius:3px;}
            QScrollBar::handle:vertical{background:#2a2a2a;border-radius:3px;}
            QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}
        """)
        self._msg_w = QtWidgets.QWidget()
        self._msg_w.setStyleSheet("background:#0a0a0a;")
        self._msg_l = QtWidgets.QVBoxLayout(self._msg_w)
        self._msg_l.setContentsMargins(14, 14, 14, 14)
        self._msg_l.setSpacing(8)
        self._msg_l.addStretch()
        self._scroll.setWidget(self._msg_w)
        root.addWidget(self._scroll, stretch=1)

        # Tool log toggle
        self._log_btn = QtWidgets.QPushButton("▸  Tool log")
        self._log_btn.setCheckable(True)
        self._log_btn.setFixedHeight(22)
        self._log_btn.setStyleSheet("""
            QPushButton{background:#0d0d0d;color:#3a3a3a;border:none;
                        border-top:1px solid #1a1a1a;text-align:left;
                        padding:0 12px;font-size:9px;font-family:'Courier New';}
            QPushButton:checked{color:#666;}
        """)
        self._log_btn.clicked.connect(lambda _: self._toggle_log())
        root.addWidget(self._log_btn)

        self._log_box = QtWidgets.QTextEdit()
        self._log_box.setReadOnly(True)
        self._log_box.setMaximumHeight(100)
        self._log_box.setFont(QtGui.QFont("Courier New", 8))
        self._log_box.setStyleSheet("QTextEdit{background:#060606;color:#3a3a3a;border:none;padding:4px 12px;}")
        self._log_box.hide()
        root.addWidget(self._log_box)

        # Quick pills
        prow = QtWidgets.QWidget()
        prow.setFixedHeight(34)
        prow.setStyleSheet("background:#0d0d0d;border-top:1px solid #181818;")
        pl = QtWidgets.QHBoxLayout(prow)
        pl.setContentsMargins(10, 0, 10, 0)
        pl.setSpacing(5)
        lbl = QtWidgets.QLabel("Quick:")
        lbl.setStyleSheet("color:#2e2e2e;font-size:9px;background:transparent;")
        pl.addWidget(lbl)
        for label, prompt in [
            ("total nodes",  "How many nodes are in my script?"),
            ("read nodes",   "How many Read nodes do I have?"),
            ("errors",       "Which nodes have errors?"),
            ("missing files","Are there any missing files?"),
            ("performance",  "What are the heaviest nodes?"),
        ]:
            b = QtWidgets.QPushButton(label)
            b.setFixedHeight(20)
            b.setStyleSheet("""
                QPushButton{background:#131313;color:#444;border:1px solid #202020;
                            border-radius:10px;padding:0 8px;font-size:9px;}
                QPushButton:hover{color:#c8f046;border-color:#c8f046;}
            """)
            b.clicked.connect(lambda _=False, p=prompt: self._send(p))
            pl.addWidget(b)
        pl.addStretch()
        root.addWidget(prow)

        # Input bar
        bar = QtWidgets.QWidget()
        bar.setFixedHeight(52)
        bar.setStyleSheet("background:#0d0d0d;border-top:1px solid #181818;")
        bl = QtWidgets.QHBoxLayout(bar)
        bl.setContentsMargins(12, 9, 12, 9)
        bl.setSpacing(7)

        self._inp = QtWidgets.QLineEdit()
        self._inp.setPlaceholderText("Ask anything about your Nuke script…")
        self._inp.setFont(QtGui.QFont("Segoe UI", 10))
        self._inp.setFixedHeight(32)
        self._inp.setStyleSheet("""
            QLineEdit{background:#161616;color:#e0e0e0;
                      border:1px solid #292929;border-radius:16px;padding:0 14px;}
            QLineEdit:focus{border-color:#c8f046;}
        """)
        self._inp.returnPressed.connect(self._on_enter)

        self._send_btn = QtWidgets.QPushButton("↑")
        self._send_btn.setFixedSize(32, 32)
        self._send_btn.setFont(QtGui.QFont("Arial", 12, QtGui.QFont.Bold))
        self._send_btn.setStyleSheet("""
            QPushButton{background:#c8f046;color:#111;border:none;border-radius:16px;}
            QPushButton:hover{background:#d8ff56;}
            QPushButton:disabled{background:#1e1e1e;color:#333;}
        """)
        self._send_btn.clicked.connect(lambda _: self._on_enter())

        bl.addWidget(self._inp, stretch=1)
        bl.addWidget(self._send_btn)
        root.addWidget(bar)

        self.setStyleSheet("QWidget{background:#0a0a0a;color:#ccc;}")

        self._add_ai_bubble(
            "Hey! I'm your Nuke AI assistant 👋\n\n"
            "Ask me anything about your script:\n"
            "• \"How many Read nodes?\"\n"
            "• \"What is the size of Blur1?\"\n"
            "• \"Change Blur1 percentage to 90\"\n"
            "• \"Create a Sharpen after Read8 and connect to viewer\"\n"
            "• \"What is the frame range of Read8?\"\n\n"
            "What do you need?"
        )

    # ── Bubbles ──────────────────────────────────────────────

    def _bubble(self, text, bg, fg, br, border="none"):
        lbl = QtWidgets.QLabel(text)
        lbl.setWordWrap(True)
        lbl.setMaximumWidth(460)
        lbl.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        lbl.setFont(QtGui.QFont("Segoe UI", 10))
        lbl.setStyleSheet(
            f"background:{bg};color:{fg};border-radius:{br};"
            f"border:{border};padding:8px 12px;"
        )
        return lbl

    def _add_user_bubble(self, text):
        row = QtWidgets.QHBoxLayout()
        row.addStretch()
        row.addWidget(self._bubble(text, "#c8f046", "#111", "14px 14px 3px 14px"))
        self._push(row)

    def _add_ai_bubble(self, text):
        row = QtWidgets.QHBoxLayout()
        icon = QtWidgets.QLabel("◈")
        icon.setFixedWidth(22)
        icon.setAlignment(QtCore.Qt.AlignTop)
        icon.setFont(QtGui.QFont("Arial", 13))
        icon.setStyleSheet("color:#c8f046;padding-top:4px;background:transparent;")
        row.addWidget(icon)
        row.addWidget(self._bubble(text, "#161616", "#d8d8d8",
                                   "14px 14px 14px 3px", "1px solid #252525"))
        row.addStretch()
        self._push(row)

    def _add_typing(self):
        row = QtWidgets.QHBoxLayout()
        icon = QtWidgets.QLabel("◈")
        icon.setFixedWidth(22)
        icon.setAlignment(QtCore.Qt.AlignTop)
        icon.setFont(QtGui.QFont("Arial", 13))
        icon.setStyleSheet("color:#c8f046;padding-top:4px;background:transparent;")
        lbl = self._bubble("thinking…", "#0e0e0e", "#3a3a3a",
                           "14px 14px 14px 3px", "1px solid #1e1e1e")
        lbl.setFont(QtGui.QFont("Courier New", 9))
        row.addWidget(icon)
        row.addWidget(lbl)
        row.addStretch()
        self._typing_lbl = lbl
        self._push(row)

    def _remove_typing(self):
        if hasattr(self, "_typing_lbl"):
            self._typing_lbl.hide()
            del self._typing_lbl

    def _push(self, layout):
        self._msg_l.insertLayout(self._msg_l.count() - 1, layout)
        QtCore.QTimer.singleShot(40, lambda: self._scroll.verticalScrollBar().setValue(
            self._scroll.verticalScrollBar().maximum()))

    # ── Send / receive ────────────────────────────────────────

    def _on_enter(self):
        text = self._inp.text().strip()
        if text:
            self._send(text)

    def _send(self, text):
        self._inp.clear()
        self._inp.setEnabled(False)
        self._send_btn.setEnabled(False)
        self._dot.setText("● thinking…")
        self._dot.setStyleSheet("color:#fa0;background:transparent;font-size:8px;")
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
        self._inp.setEnabled(True)
        self._send_btn.setEnabled(True)
        self._inp.setFocus()
        self._dot.setText("● ready")
        self._dot.setStyleSheet("color:#3c3;background:transparent;font-size:8px;")

    def _toggle_log(self):
        on = self._log_btn.isChecked()
        self._log_box.setVisible(on)
        self._log_btn.setText("▾  Tool log" if on else "▸  Tool log")

    def _new_chat(self):
        self.ai.reset()
        while self._msg_l.count() > 1:
            item = self._msg_l.takeAt(0)
            if item.layout():
                while item.layout().count():
                    w = item.layout().takeAt(0).widget()
                    if w:
                        w.deleteLater()
        self._log_box.clear()
        self._add_ai_bubble("Fresh start! What can I help you with?")


# ─────────────────────────────────────────────────────────────────────────────
#  NUKE PANEL WRAPPER  (registers with Nuke's panel system)
# ─────────────────────────────────────────────────────────────────────────────

if NUKE_ENV:
    class NukeAIPanel(nukescripts.PythonPanel):
        """
        Proper Nuke panel — docks into the interface like any other Nuke panel.
        Access via: Windows menu → Custom → Nuke AI Assistant
        """
        def __init__(self):
            nukescripts.PythonPanel.__init__(
                self,
                "Nuke AI Assistant",
                "com.nukeai.assistant"
            )
            self._widget = NukeAIWidget()
            self.customKnob = nuke.PyCustom_Knob(
                "Nuke AI", "", "nuke_ai_assistant._get_widget()"
            )
            self.addKnob(self.customKnob)

        def makeUI(self):
            return self._widget


# =============================================================================
#  HOW THIS WORKS:
#
#  1. Paste this whole file into Nuke's Script Editor → Run (Ctrl+Enter)
#  2. A node called "AI_Assistant" appears in your node graph
#  3. Click (select) that node → its Properties tab opens in the Properties panel
#  4. The AI chat lives inside the Properties tab — just like any Nuke node
#
#  The Properties panel integration works via nuke.PyCustom_Knob which
#  embeds a live Qt widget directly into Nuke's property panel.
# =============================================================================


# ─────────────────────────────────────────────────────────────────────────────
#  GLOBAL AI INSTANCE  (one shared instance across the whole session)
# ─────────────────────────────────────────────────────────────────────────────

_ai_instance = None

def _get_ai():
    global _ai_instance
    if _ai_instance is None:
        _ai_instance = NukeAI()
    return _ai_instance


# ─────────────────────────────────────────────────────────────────────────────
#  KNOB WIDGET FACTORY
#  Nuke calls this string as Python when building the Properties panel.
#  It must return a QWidget.  We point it at a module-level function so
#  Nuke can evaluate it by name.
# ─────────────────────────────────────────────────────────────────────────────

def _make_properties_widget():
    """
    Called by Nuke when it builds the node's Properties panel.
    Returns the NukeAIWidget that becomes the body of the Properties tab.
    """
    w = NukeAIWidget()
    # Share the global AI instance so chat history persists
    w.ai = _get_ai()
    return w


# ─────────────────────────────────────────────────────────────────────────────
#  THE GRAPH NODE
# ─────────────────────────────────────────────────────────────────────────────

def _create_ai_node():
    """
    Creates (or re-selects) the AI_Assistant node in the node graph.

    The node uses a PyCustom_Knob whose value is a Python expression
    that Nuke evaluates to get a QWidget.  That widget IS the Properties panel.

    So:  click node  →  Properties panel opens  →  shows the AI chat widget.
    """
    NODE_NAME = "AI_Assistant"

    # ── If node already exists just re-open its properties ───
    existing = nuke.toNode(NODE_NAME)
    if existing:
        for n in nuke.allNodes():
            n["selected"].setValue(False)
        existing["selected"].setValue(True)
        existing.showControlPanel()
        return existing

    # ── Create a NoOp as the base ─────────────────────────────
    # NoOp is the standard choice for utility/tool nodes in Nuke —
    # it passes through any connected input unchanged.
    node = nuke.nodes.NoOp()
    node["name"].setValue(NODE_NAME)

    # ── Appearance in the graph ───────────────────────────────
    node["tile_color"].setValue(0x1a1a2eff)   # dark indigo
    node["gl_color"].setValue(0xc8f046ff)      # lime green border
    node["label"].setValue(
        "<center><b><font color='#c8f046' size='4'>◈</font></b></center>"
        "<center><font size='2'>AI Assistant</font></center>"
    )
    node["note_font_size"].setValue(10)

    # ── PyCustom_Knob — this is what embeds the chat widget ───
    # Nuke evaluates the string as a Python expression each time
    # the Properties panel is opened.  It must return a QWidget.
    #
    # We call _make_properties_widget() which is defined above
    # and already loaded in this Nuke session.
    knob = nuke.PyCustom_Knob(
        "ai_chat",           # internal knob name
        "",                  # label (blank — widget fills full width)
        "__import__('nuke_ai_assistant', fromlist=['_make_properties_widget'])._make_properties_widget()"
        if False else        # use module import if saved as file
        "_make_properties_widget()"  # use in-session function (Script Editor mode)
    )
    node.addKnob(knob)

    # ── Position: top-left of graph, out of the way ───────────
    node.setXYpos(-300, -300)

    # ── Select it and open its Properties right away ──────────
    for n in nuke.allNodes():
        n["selected"].setValue(False)
    node["selected"].setValue(True)
    node.showControlPanel()

    return node


# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if NUKE_ENV:
    _create_ai_node()
