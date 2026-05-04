# ============================================================
# NUKE AI ASSISTANT - with GUI Panel
# Run inside Nuke's Script Editor or add to menu.py
# Requires: Ollama running locally with qwen2.5-coder:7b
# ============================================================

import requests
import json
import os
import re
from datetime import datetime

try:
    import nuke
    import nukescripts
    from PySide2 import QtWidgets, QtCore, QtGui
    NUKE_ENV = True
except ImportError:
    NUKE_ENV = False
    print("Not running inside Nuke — GUI preview only")


# ============================================================
# NUKE AI CORE ENGINE
# ============================================================

class NukeAI:
    """AI-powered Nuke automation engine"""

    def __init__(self, model="qwen2.5-coder:7b"):
        self.model = model
        self.api_url = "http://localhost:11434/api/generate"
        self.history = []
        self.last_created_nodes = []

    def ai_nuke(self, prompt, context=None, callback=None):
        """
        Main entry point.
        callback(msg) is called with status strings so the GUI can display them.
        Returns a result dict: {"success": bool, "output": str, "data": any}
        """
        def log(msg):
            print(msg)
            if callback:
                callback(msg)

        if context:
            prompt = f"Context: {context}\n\nRequest: {prompt}"

        log(f"🤖 Thinking about: {prompt}")
        system_prompt = self._build_system_prompt()
        raw = self._call_llm(system_prompt, prompt, log)

        if raw is None:
            return {"success": False, "output": "❌ LLM returned no response.", "data": None}

        result = self._execute_command(raw, log)

        self.history.append({
            "prompt": prompt,
            "response": raw,
            "result": result,
            "timestamp": datetime.now().isoformat()
        })
        return result

    # ----------------------------------------------------------
    # SYSTEM PROMPT
    # ----------------------------------------------------------

    def _build_system_prompt(self):
        return """
You are a Nuke VFX automation assistant. Convert the user's natural language request into a single JSON command.
Return ONLY raw JSON — no markdown, no explanation, no code fences.

SUPPORTED ACTIONS:

── QUERY ────────────────────────────────────────────────────
{"action":"query","scope":"script","question":"total_nodes"}
{"action":"query","scope":"script","question":"script_stats"}
{"action":"query","scope":"script","question":"all_node_names"}
{"action":"query","scope":"script","question":"node_tree"}
{"action":"query","scope":"script","question":"missing_files"}
{"action":"query","scope":"script","question":"disabled_nodes"}
{"action":"query","scope":"script","question":"error_nodes"}
{"action":"query","scope":"node","target":"Read1","question":"frame_range"}
{"action":"query","scope":"node","target":"Read1","question":"file_path"}
{"action":"query","scope":"node","target":"Read1","question":"connections"}
{"action":"query","scope":"node","target":"Read1","question":"all_knobs"}

IMPORTANT: For questions like "how many Read nodes", "count total nodes", "count all blur nodes"
→ use {"action":"query","scope":"script","question":"script_stats"}
The script_stats action returns counts by type which answers all counting questions.

── CREATE ───────────────────────────────────────────────────
{"action":"create","node":{"id":"Blur1","type":"Blur","input":"Read1","knobs":{"size":10}}}
{"action":"create_tree","nodes":[
  {"id":"Grade1","type":"Grade","input":"Read1"},
  {"id":"Blur1","type":"Blur","input":"Grade1","knobs":{"size":5}}
]}
{"action":"smart_setup","pattern":"beauty_comp","source":"Read1"}
{"action":"smart_setup","pattern":"color_correct","source":"Read1"}
{"action":"smart_setup","pattern":"denoise_sharpen","source":"Read1"}
{"action":"copy_setup","source":"Grade1","targets":["Grade2","Grade3"]}

── MODIFY ───────────────────────────────────────────────────
{"action":"set","target":"Blur1","knobs":{"size":20}}
{"action":"batch_set","filter":{"type":"Grade"},"knobs":{"white":0.5}}
{"action":"batch_set","filter":{"name_contains":"Beauty"},"knobs":{"disable":true}}
{"action":"batch_rename","filter":{"type":"Read"},"pattern":"src_{index}"}

── CONNECT / DELETE / RENAME ────────────────────────────────
{"action":"connect","source":"Grade1","target":"Blur1","input_index":0}
{"action":"delete","target":"Blur1"}
{"action":"rename","target":"Blur1","new_name":"SoftBlur"}
{"action":"select","targets":["Grade1","Blur1"]}
{"action":"duplicate","target":"Grade1"}

── ORGANIZE ─────────────────────────────────────────────────
{"action":"organize","method":"by_type"}
{"action":"organize","method":"horizontal_flow"}
{"action":"organize","method":"vertical_stack"}

── FILE OPERATIONS ──────────────────────────────────────────
{"action":"update_paths","old_prefix":"/old/path","new_prefix":"/new/path"}
{"action":"validate_paths"}
{"action":"find_sequences","directory":"/renders"}
{"action":"setup_write","name":"beauty_v01","path":"/output/beauty.####.exr","format":"exr"}

── ANALYSIS ─────────────────────────────────────────────────
{"action":"analyze_performance"}
{"action":"find_bottlenecks"}
{"action":"validate_script"}

── UTILITIES ────────────────────────────────────────────────
{"action":"snapshot","name":"before_grade"}
{"action":"backdrop","targets":["Grade1","Blur1"],"label":"GRADE STACK"}

NUKE CLASS NAMES (exact):
- Backdrop → BackdropNode
- Merge    → Merge2
- Shuffle  → Shuffle2

Return valid JSON only. For ambiguous requests, choose the most common interpretation.
"""

    # ----------------------------------------------------------
    # LLM CALL
    # ----------------------------------------------------------

    def _call_llm(self, system_prompt, user_prompt, log):
        try:
            r = requests.post(
                self.api_url,
                json={
                    "model": self.model,
                    "prompt": system_prompt + "\n\nUser Request:\n" + user_prompt,
                    "stream": False,
                    "options": {
                        "temperature": 0.1,
                        "num_predict": 400,
                        "num_ctx": 4096,
                        "top_k": 20,
                        "top_p": 0.9
                    }
                },
                timeout=60
            )
            text = r.json().get("response", "")
            text = text.replace("```json", "").replace("```", "").strip()

            # Extract first JSON object if extra text leaked through
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                text = match.group(0)

            log(f"📋 Command: {text}")
            return json.loads(text)

        except requests.exceptions.ConnectionError:
            log("❌ Cannot connect to Ollama. Is it running? (ollama serve)")
            return None
        except json.JSONDecodeError as e:
            log(f"❌ JSON parse error: {e}\nRaw: {text}")
            return None
        except Exception as e:
            log(f"❌ LLM Error: {e}")
            return None

    # ----------------------------------------------------------
    # COMMAND ROUTER
    # ----------------------------------------------------------

    def _execute_command(self, data, log):
        if not isinstance(data, dict) or "action" not in data:
            log("❌ Invalid command format")
            return {"success": False, "output": "Invalid command", "data": None}

        action = data["action"]
        handlers = {
            "query":              self._handle_query,
            "create":             self._handle_create,
            "create_tree":        self._handle_create_tree,
            "smart_setup":        self._handle_smart_setup,
            "copy_setup":         self._handle_copy_setup,
            "set":                self._handle_set,
            "batch_set":          self._handle_batch_set,
            "batch_rename":       self._handle_batch_rename,
            "connect":            self._handle_connect,
            "delete":             self._handle_delete,
            "rename":             self._handle_rename,
            "select":             self._handle_select,
            "move":               self._handle_move,
            "duplicate":          self._handle_duplicate,
            "backdrop":           self._handle_backdrop,
            "organize":           self._handle_organize,
            "update_paths":       self._handle_update_paths,
            "validate_paths":     self._handle_validate_paths,
            "find_sequences":     self._handle_find_sequences,
            "setup_write":        self._handle_setup_write,
            "analyze_performance":self._handle_analyze_performance,
            "find_bottlenecks":   self._handle_find_bottlenecks,
            "validate_script":    self._handle_validate_script,
            "snapshot":           self._handle_snapshot,
        }

        handler = handlers.get(action)
        if handler:
            try:
                result = handler(data, log)
                return result
            except Exception as e:
                log(f"❌ Handler error: {e}")
                return {"success": False, "output": str(e), "data": None}
        else:
            log(f"❌ Unknown action: {action}")
            return {"success": False, "output": f"Unknown action: {action}", "data": None}

    # ----------------------------------------------------------
    # QUERY HANDLERS
    # ----------------------------------------------------------

    def _handle_query(self, data, log):
        scope = data.get("scope", "node")
        question = data["question"]
        if scope == "script":
            return self._query_script(question, log)
        elif scope == "node":
            target = data.get("target", "")
            return self._query_node(target, question, log)
        return {"success": False, "output": "Unknown scope", "data": None}

    def _query_script(self, question, log):
        if not NUKE_ENV:
            return {"success": False, "output": "Not in Nuke", "data": None}

        if question == "total_nodes":
            count = len(nuke.allNodes())
            msg = f"📊 Total Nodes: {count}"
            log(msg)
            return {"success": True, "output": msg, "data": count}

        elif question == "all_node_names":
            names = [n.name() for n in nuke.allNodes()]
            msg = "📋 All Nodes:\n" + "\n".join(f"  • {n}" for n in names)
            log(msg)
            return {"success": True, "output": msg, "data": names}

        elif question == "script_stats":
            stats = {"total_nodes": len(nuke.allNodes()), "by_type": {}}
            for n in nuke.allNodes():
                t = n.Class()
                stats["by_type"][t] = stats["by_type"].get(t, 0) + 1

            lines = [f"📊 Script Statistics:", f"  Total: {stats['total_nodes']} nodes", "  By type:"]
            for k, v in sorted(stats["by_type"].items(), key=lambda x: -x[1]):
                lines.append(f"    {k}: {v}")
            msg = "\n".join(lines)
            log(msg)
            return {"success": True, "output": msg, "data": stats}

        elif question == "node_tree":
            tree = self._build_node_tree()
            msg = "🌳 Node Tree:\n" + "\n".join(tree)
            log(msg)
            return {"success": True, "output": msg, "data": tree}

        elif question == "missing_files":
            missing = []
            for n in nuke.allNodes():
                if n.Class() in ["Read", "ReadGeo", "Camera", "Write"]:
                    if "file" in n.knobs():
                        path = n["file"].value()
                        if path:
                            try:
                                resolved = nuke.filename(n)
                                if resolved and not os.path.exists(resolved):
                                    missing.append({"node": n.name(), "path": path})
                            except:
                                pass
            if missing:
                lines = [f"⚠️  Missing Files ({len(missing)}):"]
                for m in missing:
                    lines.append(f"  ✗ {m['node']}: {m['path']}")
            else:
                lines = ["✅ All file paths are valid"]
            msg = "\n".join(lines)
            log(msg)
            return {"success": True, "output": msg, "data": missing}

        elif question == "error_nodes":
            errors = [(n.name(), n.error()) for n in nuke.allNodes() if n.hasError()]
            if errors:
                lines = [f"🔴 Nodes with Errors ({len(errors)}):"]
                for name, err in errors:
                    lines.append(f"  ✗ {name}: {err}")
            else:
                lines = ["✅ No nodes have errors"]
            msg = "\n".join(lines)
            log(msg)
            return {"success": True, "output": msg, "data": errors}

        elif question == "disabled_nodes":
            # FIX: use n.knob() not n.knb()
            disabled = [n.name() for n in nuke.allNodes()
                        if n.knob("disable") and n["disable"].value()]
            if disabled:
                msg = f"⏸️  Disabled Nodes ({len(disabled)}):\n" + "\n".join(f"  • {d}" for d in disabled)
            else:
                msg = "✅ No disabled nodes"
            log(msg)
            return {"success": True, "output": msg, "data": disabled}

        return {"success": False, "output": f"Unknown question: {question}", "data": None}

    def _query_node(self, target, question, log):
        if not NUKE_ENV:
            return {"success": False, "output": "Not in Nuke", "data": None}
        node = nuke.toNode(target)
        if not node:
            msg = f"❌ Node not found: {target}"
            log(msg)
            return {"success": False, "output": msg, "data": None}

        if question == "connections":
            inputs = [node.input(i).name() if node.input(i) else None for i in range(node.inputs())]
            # FIX: dependent() lists nodes that depend ON this node (downstream)
            outputs = [dep.name() for dep in node.dependent()]
            data = {"inputs": inputs, "outputs": outputs}
            msg = f"🔗 {target} connections:\n  Inputs: {inputs}\n  Outputs: {outputs}"
            log(msg)
            return {"success": True, "output": msg, "data": data}

        elif question == "all_knobs":
            knobs = {}
            for k in node.allKnobs():
                try:
                    knobs[k.name()] = str(k.value())
                except:
                    knobs[k.name()] = "<complex>"
            msg = f"🎛️  Knobs for {target}:\n" + "\n".join(f"  {k}: {v}" for k, v in knobs.items())
            log(msg)
            return {"success": True, "output": msg, "data": knobs}

        elif question == "frame_range":
            if "first" in node.knobs() and "last" in node.knobs():
                fr = f"{int(node['first'].value())}-{int(node['last'].value())}"
                msg = f"🎬 Frame Range: {fr}"
                log(msg)
                return {"success": True, "output": msg, "data": fr}

        elif question == "file_path":
            if "file" in node.knobs():
                path = node["file"].value()
                msg = f"📁 File: {path}"
                log(msg)
                return {"success": True, "output": msg, "data": path}

        return {"success": False, "output": f"Unknown question: {question}", "data": None}

    # ----------------------------------------------------------
    # CREATE HANDLERS
    # ----------------------------------------------------------

    def _handle_create(self, data, log):
        if not NUKE_ENV:
            return {"success": False, "output": "Not in Nuke", "data": None}
        nd = data["node"]
        node_type = self._normalize_node_type(nd["type"])
        try:
            node = getattr(nuke.nodes, node_type)()
            node["name"].setValue(nd["id"])
            if "input" in nd:
                src = nuke.toNode(nd["input"])
                if src:
                    node.setInput(0, src)
            for k, v in nd.get("knobs", {}).items():
                if k in node.knobs():
                    try:
                        node[k].setValue(v)
                    except:
                        pass
            msg = f"✅ Created node: {node.name()} ({node_type})"
            log(msg)
            self.last_created_nodes.append(node.name())
            return {"success": True, "output": msg, "data": node.name()}
        except Exception as e:
            msg = f"❌ Create failed: {e}"
            log(msg)
            return {"success": False, "output": msg, "data": None}

    def _handle_create_tree(self, data, log):
        created = []
        for nd in data["nodes"]:
            r = self._handle_create({"node": nd}, log)
            if r["success"]:
                created.append(r["data"])
        msg = f"✅ Created tree: {' → '.join(created)}"
        log(msg)
        return {"success": True, "output": msg, "data": created}

    def _handle_smart_setup(self, data, log):
        if not NUKE_ENV:
            return {"success": False, "output": "Not in Nuke", "data": None}
        pattern = data["pattern"]
        source = data["source"]
        src_node = nuke.toNode(source)
        if not src_node:
            msg = f"❌ Source node not found: {source}"
            log(msg)
            return {"success": False, "output": msg, "data": None}

        patterns = {
            "beauty_comp": [
                {"type": "Grade",   "id": f"{source}_Grade"},
                {"type": "Blur",    "id": f"{source}_Blur",    "knobs": {"size": 1}},
                {"type": "Sharpen", "id": f"{source}_Sharpen"},
            ],
            "color_correct": [
                {"type": "ColorCorrect", "id": f"{source}_CC"},
                {"type": "Saturation",   "id": f"{source}_Sat"},
                {"type": "HueCorrect",   "id": f"{source}_Hue"},
            ],
            "denoise_sharpen": [
                {"type": "Denoise",  "id": f"{source}_Denoise"},
                {"type": "Sharpen",  "id": f"{source}_Sharpen"},
                {"type": "EdgeBlur", "id": f"{source}_Edge"},
            ],
        }

        if pattern not in patterns:
            msg = f"❌ Unknown pattern: {pattern}. Available: {list(patterns.keys())}"
            log(msg)
            return {"success": False, "output": msg, "data": None}

        prev = src_node
        created = []
        for spec in patterns[pattern]:
            spec["input"] = prev.name()
            r = self._handle_create({"node": spec}, log)
            if r["success"]:
                created.append(r["data"])
                prev = nuke.toNode(r["data"])

        msg = f"✅ Smart setup '{pattern}': {' → '.join(created)}"
        log(msg)
        return {"success": True, "output": msg, "data": created}

    # ----------------------------------------------------------
    # MODIFY / BATCH
    # ----------------------------------------------------------

    def _handle_set(self, data, log):
        if not NUKE_ENV:
            return {"success": False, "output": "Not in Nuke", "data": None}
        node = nuke.toNode(data["target"])
        if not node:
            msg = f"❌ Node not found: {data['target']}"
            log(msg)
            return {"success": False, "output": msg, "data": None}
        changed = []
        for k, v in data["knobs"].items():
            if k in node.knobs():
                try:
                    node[k].setValue(v)
                    changed.append(f"{k}={v}")
                except Exception as e:
                    log(f"  ⚠️  Could not set {k}: {e}")
        msg = f"✅ Updated {node.name()}: {', '.join(changed)}"
        log(msg)
        return {"success": True, "output": msg, "data": changed}

    def _handle_batch_set(self, data, log):
        if not NUKE_ENV:
            return {"success": False, "output": "Not in Nuke", "data": None}
        nodes = self._filter_nodes(data["filter"])
        for node in nodes:
            for k, v in data["knobs"].items():
                if k in node.knobs():
                    try:
                        node[k].setValue(v)
                    except:
                        pass
        msg = f"✅ Batch modified {len(nodes)} nodes"
        log(msg)
        return {"success": True, "output": msg, "data": [n.name() for n in nodes]}

    def _handle_batch_rename(self, data, log):
        if not NUKE_ENV:
            return {"success": False, "output": "Not in Nuke", "data": None}
        nodes = self._filter_nodes(data["filter"])
        pattern = data["pattern"]
        renamed = []
        for i, node in enumerate(nodes, 1):
            new_name = pattern.replace("{index}", str(i)).replace("{type}", node.Class())
            node["name"].setValue(new_name)
            renamed.append(node.name())
        msg = f"✅ Renamed {len(renamed)} nodes"
        log(msg)
        return {"success": True, "output": msg, "data": renamed}

    def _handle_connect(self, data, log):
        if not NUKE_ENV:
            return {"success": False, "output": "Not in Nuke", "data": None}
        src = nuke.toNode(data["source"])
        dst = nuke.toNode(data["target"])
        if src and dst:
            dst.setInput(data.get("input_index", 0), src)
            msg = f"✅ Connected: {src.name()} → {dst.name()}"
            log(msg)
            return {"success": True, "output": msg, "data": None}
        msg = f"❌ Connect failed: nodes not found"
        log(msg)
        return {"success": False, "output": msg, "data": None}

    def _handle_delete(self, data, log):
        if not NUKE_ENV:
            return {"success": False, "output": "Not in Nuke", "data": None}
        node = nuke.toNode(data["target"])
        if node:
            name = node.name()
            nuke.delete(node)
            msg = f"✅ Deleted: {name}"
            log(msg)
            return {"success": True, "output": msg, "data": name}
        msg = f"❌ Node not found: {data['target']}"
        log(msg)
        return {"success": False, "output": msg, "data": None}

    def _handle_rename(self, data, log):
        if not NUKE_ENV:
            return {"success": False, "output": "Not in Nuke", "data": None}
        node = nuke.toNode(data["target"])
        if node:
            node["name"].setValue(data["new_name"])
            msg = f"✅ Renamed to: {data['new_name']}"
            log(msg)
            return {"success": True, "output": msg, "data": data["new_name"]}
        msg = f"❌ Node not found: {data['target']}"
        log(msg)
        return {"success": False, "output": msg, "data": None}

    def _handle_select(self, data, log):
        if not NUKE_ENV:
            return {"success": False, "output": "Not in Nuke", "data": None}
        for n in nuke.allNodes():
            if n.knob("selected"):
                n["selected"].setValue(False)
        selected = []
        for name in data["targets"]:
            node = nuke.toNode(name)
            if node and node.knob("selected"):
                node["selected"].setValue(True)
                selected.append(name)
        msg = f"✅ Selected: {selected}"
        log(msg)
        return {"success": True, "output": msg, "data": selected}

    def _handle_move(self, data, log):
        if not NUKE_ENV:
            return {"success": False, "output": "Not in Nuke", "data": None}
        node = nuke.toNode(data["target"])
        if node:
            node.setXYpos(data["xpos"], data["ypos"])
            msg = f"✅ Moved: {node.name()} to ({data['xpos']}, {data['ypos']})"
            log(msg)
            return {"success": True, "output": msg, "data": None}
        return {"success": False, "output": "Node not found", "data": None}

    def _handle_duplicate(self, data, log):
        if not NUKE_ENV:
            return {"success": False, "output": "Not in Nuke", "data": None}
        node = nuke.toNode(data["target"])
        if node:
            for n in nuke.allNodes():
                if n.knob("selected"):
                    n["selected"].setValue(False)
            node["selected"].setValue(True)
            nuke.nodeCopy("%clipboard%")
            new_node = nuke.nodePaste("%clipboard%")
            msg = f"✅ Duplicated: {new_node.name()}"
            log(msg)
            return {"success": True, "output": msg, "data": new_node.name()}
        return {"success": False, "output": "Node not found", "data": None}

    def _handle_backdrop(self, data, log):
        if not NUKE_ENV:
            return {"success": False, "output": "Not in Nuke", "data": None}
        targets = [nuke.toNode(n) for n in data["targets"]]
        targets = [n for n in targets if n]
        if not targets:
            return {"success": False, "output": "No valid nodes", "data": None}
        for n in nuke.allNodes():
            if n.knob("selected"):
                n["selected"].setValue(False)
        for n in targets:
            n["selected"].setValue(True)
        bd = nuke.nodes.BackdropNode()
        bd["label"].setValue(data.get("label", "BACKDROP"))
        msg = f"✅ Backdrop created: {bd.name()}"
        log(msg)
        return {"success": True, "output": msg, "data": bd.name()}

    # ----------------------------------------------------------
    # ORGANIZE
    # ----------------------------------------------------------

    def _handle_organize(self, data, log):
        if not NUKE_ENV:
            return {"success": False, "output": "Not in Nuke", "data": None}
        method = data["method"]
        if method == "by_type":
            nodes_by_type = {}
            for n in nuke.allNodes():
                t = n.Class()
                nodes_by_type.setdefault(t, []).append(n)
            y = 0
            for t, nodes in sorted(nodes_by_type.items()):
                for i, node in enumerate(nodes):
                    node.setXYpos(i * 160, y)
                y += 160
        elif method == "horizontal_flow":
            for i, n in enumerate(nuke.allNodes()):
                n.setXYpos(i * 160, 0)
        elif method == "vertical_stack":
            for i, n in enumerate(nuke.allNodes()):
                n.setXYpos(0, i * 110)
        msg = f"✅ Organized nodes: {method}"
        log(msg)
        return {"success": True, "output": msg, "data": None}

    # ----------------------------------------------------------
    # FILE OPERATIONS
    # ----------------------------------------------------------

    def _handle_update_paths(self, data, log):
        if not NUKE_ENV:
            return {"success": False, "output": "Not in Nuke", "data": None}
        old_p = data["old_prefix"]
        new_p = data["new_prefix"]
        updated = []
        for n in nuke.allNodes():
            if "file" in n.knobs():
                old_path = n["file"].value()
                if old_path and old_path.startswith(old_p):
                    n["file"].setValue(old_path.replace(old_p, new_p, 1))
                    updated.append(n.name())
        msg = f"✅ Updated {len(updated)} file paths"
        log(msg)
        return {"success": True, "output": msg, "data": updated}

    def _handle_validate_paths(self, data, log):
        return self._query_script("missing_files", log)

    def _handle_find_sequences(self, data, log):
        directory = data["directory"]
        if not os.path.exists(directory):
            msg = f"❌ Directory not found: {directory}"
            log(msg)
            return {"success": False, "output": msg, "data": None}
        sequences = {}
        for filename in os.listdir(directory):
            match = re.match(r"(.+?)(\d{4,})\.(exr|dpx|jpg|png|tif|tiff)$", filename)
            if match:
                base, frame, ext = match.groups()
                key = f"{base}####.{ext}"
                sequences.setdefault(key, []).append(int(frame))
        lines = [f"📁 Found {len(sequences)} sequences:"]
        for seq, frames in sorted(sequences.items()):
            lines.append(f"  {seq}: {min(frames)}-{max(frames)} ({len(frames)} frames)")
        msg = "\n".join(lines)
        log(msg)
        return {"success": True, "output": msg, "data": sequences}

    def _handle_setup_write(self, data, log):
        if not NUKE_ENV:
            return {"success": False, "output": "Not in Nuke", "data": None}
        name = data["name"]
        path = data["path"]
        fmt  = data.get("format", "exr")
        write = nuke.nodes.Write()
        write["name"].setValue(name)
        write["file"].setValue(path)
        write["file_type"].setValue(fmt)
        if fmt == "exr":
            if "compression" in write.knobs():
                write["compression"].setValue("Zip (1 scanline)")
            if "datatype" in write.knobs():
                write["datatype"].setValue("16 bit half")
        msg = f"✅ Write node created: {write.name()} → {path}"
        log(msg)
        return {"success": True, "output": msg, "data": write.name()}

    # ----------------------------------------------------------
    # ANALYSIS
    # ----------------------------------------------------------

    def _handle_analyze_performance(self, data, log):
        if not NUKE_ENV:
            return {"success": False, "output": "Not in Nuke", "data": None}
        heavy = []
        warnings = []
        for n in nuke.allNodes():
            if n.Class() == "Blur" and "size" in n.knobs():
                s = n["size"].value()
                if s > 50:
                    heavy.append(f"{n.name()} (Blur size={s})")
            if "samples" in n.knobs():
                s = n["samples"].value()
                if s > 10:
                    warnings.append(f"{n.name()} has {s} samples")

        lines = ["⚡ Performance Analysis:"]
        if heavy:
            lines.append(f"  Heavy nodes ({len(heavy)}):")
            lines += [f"    • {h}" for h in heavy]
        if warnings:
            lines.append(f"  Warnings ({len(warnings)}):")
            lines += [f"    • {w}" for w in warnings]
        if not heavy and not warnings:
            lines.append("  ✅ No major performance issues found")
        msg = "\n".join(lines)
        log(msg)
        return {"success": True, "output": msg, "data": {"heavy": heavy, "warnings": warnings}}

    def _handle_find_bottlenecks(self, data, log):
        if not NUKE_ENV:
            return {"success": False, "output": "Not in Nuke", "data": None}
        heavy_classes = ["Denoise", "Defocus", "ZDefocus", "DeepToImage", "SphericalTransform",
                         "VectorBlur", "Kronos", "MotionBlur2D"]
        bottlenecks = [{"node": n.name(), "type": n.Class()}
                       for n in nuke.allNodes() if n.Class() in heavy_classes]
        if bottlenecks:
            lines = [f"🐌 Bottlenecks ({len(bottlenecks)}):"]
            for b in bottlenecks:
                lines.append(f"  • {b['node']} ({b['type']})")
        else:
            lines = ["✅ No heavy bottleneck nodes found"]
        msg = "\n".join(lines)
        log(msg)
        return {"success": True, "output": msg, "data": bottlenecks}

    def _handle_validate_script(self, data, log):
        if not NUKE_ENV:
            return {"success": False, "output": "Not in Nuke", "data": None}
        issues = {"errors": [], "warnings": [], "info": []}
        for n in nuke.allNodes():
            if n.hasError():
                issues["errors"].append(f"{n.name()}: {n.error()}")
        missing_r = self._query_script("missing_files", log)
        if missing_r["data"]:
            issues["warnings"] += [f"Missing: {m['node']}" for m in missing_r["data"]]
        disabled_r = self._query_script("disabled_nodes", log)
        if disabled_r["data"]:
            issues["info"].append(f"{len(disabled_r['data'])} nodes disabled")
        lines = ["🔍 Script Validation:"]
        if issues["errors"]:
            lines.append(f"  🔴 Errors ({len(issues['errors'])}):")
            lines += [f"    • {e}" for e in issues["errors"]]
        if issues["warnings"]:
            lines.append(f"  🟡 Warnings ({len(issues['warnings'])}):")
            lines += [f"    • {w}" for w in issues["warnings"]]
        if issues["info"]:
            lines.append(f"  ℹ️  Info:")
            lines += [f"    • {i}" for i in issues["info"]]
        if not any(issues.values()):
            lines.append("  ✅ Script is healthy")
        msg = "\n".join(lines)
        log(msg)
        return {"success": True, "output": msg, "data": issues}

    # ----------------------------------------------------------
    # UTILITIES
    # ----------------------------------------------------------

    def _handle_snapshot(self, data, log):
        if not NUKE_ENV:
            return {"success": False, "output": "Not in Nuke", "data": None}
        script_path = nuke.root().name()
        if script_path in ("Root", ""):
            msg = "❌ Script not saved yet — please save first"
            log(msg)
            return {"success": False, "output": msg, "data": None}
        snap_dir = os.path.join(os.path.dirname(script_path), "snapshots")
        os.makedirs(snap_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        snap_path = os.path.join(snap_dir, f"{data['name']}_{ts}.nk")
        nuke.scriptSave(snap_path)
        msg = f"📸 Snapshot saved: {snap_path}"
        log(msg)
        return {"success": True, "output": msg, "data": snap_path}

    def _handle_copy_setup(self, data, log):
        if not NUKE_ENV:
            return {"success": False, "output": "Not in Nuke", "data": None}
        source = nuke.toNode(data["source"])
        if not source:
            msg = f"❌ Source node not found: {data['source']}"
            log(msg)
            return {"success": False, "output": msg, "data": None}
        skip = {"name", "xpos", "ypos", "selected"}
        knob_values = {}
        for k in source.allKnobs():
            if k.name() not in skip:
                try:
                    knob_values[k.name()] = k.value()
                except:
                    pass
        updated = []
        for t_name in data["targets"]:
            target = nuke.toNode(t_name)
            if target:
                for k_name, k_val in knob_values.items():
                    if k_name in target.knobs():
                        try:
                            target[k_name].setValue(k_val)
                        except:
                            pass
                updated.append(t_name)
        msg = f"✅ Copied setup to: {', '.join(updated)}"
        log(msg)
        return {"success": True, "output": msg, "data": updated}

    # ----------------------------------------------------------
    # HELPERS
    # ----------------------------------------------------------

    def _normalize_node_type(self, node_type):
        aliases = {"Backdrop": "BackdropNode", "Merge": "Merge2", "Shuffle": "Shuffle2"}
        return aliases.get(node_type, node_type)

    def _filter_nodes(self, node_filter):
        if not NUKE_ENV:
            return []
        nodes = nuke.allNodes()
        if "type" in node_filter:
            nodes = [n for n in nodes if n.Class() == node_filter["type"]]
        if "name_contains" in node_filter:
            nodes = [n for n in nodes if node_filter["name_contains"] in n.name()]
        if node_filter.get("selected"):
            nodes = nuke.selectedNodes()
        return nodes

    def _build_node_tree(self):
        if not NUKE_ENV:
            return []
        tree = []
        visited = set()

        def traverse(node, depth=0):
            if node.name() in visited:
                return
            visited.add(node.name())
            indent = "  " * depth
            tree.append(f"{indent}├─ {node.name()} ({node.Class()})")
            for dep in node.dependent():
                traverse(dep, depth + 1)

        for n in nuke.allNodes():
            if n.Class() == "Read":
                traverse(n)
        # Also catch nodes with no inputs
        for n in nuke.allNodes():
            if n.name() not in visited:
                traverse(n)
        return tree


# ============================================================
# GUI PANEL
# ============================================================

class NukeAIPanel(QtWidgets.QWidget):
    """Nuke AI Assistant GUI Panel"""

    # Signal to append text from background thread safely
    append_signal = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ai = NukeAI()
        self.setWindowTitle("🤖 Nuke AI Assistant")
        self.setMinimumSize(680, 520)
        self._build_ui()
        self.append_signal.connect(self._append_to_output)
        self._log("Welcome to Nuke AI Assistant!")
        self._log("Type a natural language command and press Enter or click Run.")
        self._log("─" * 55)

    def _build_ui(self):
        # ── Fonts ──
        mono = QtGui.QFont("Courier New", 9)
        ui_font = QtGui.QFont("Segoe UI", 10)

        # ── Root layout ──
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        # ── Title bar ──
        title_bar = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("🤖  Nuke AI Assistant")
        title.setFont(QtGui.QFont("Segoe UI", 13, QtGui.QFont.Bold))
        title.setStyleSheet("color: #e8c87a;")

        model_label = QtWidgets.QLabel("Model:")
        model_label.setFont(ui_font)
        model_label.setStyleSheet("color: #aaa;")

        self.model_combo = QtWidgets.QComboBox()
        self.model_combo.addItems([
            "qwen2.5-coder:7b",
            "llama3.2:3b",
            "mistral:7b",
            "codellama:7b",
        ])
        self.model_combo.setFont(ui_font)
        self.model_combo.currentTextChanged.connect(self._on_model_changed)
        self.model_combo.setFixedWidth(180)

        title_bar.addWidget(title)
        title_bar.addStretch()
        title_bar.addWidget(model_label)
        title_bar.addWidget(self.model_combo)
        root.addLayout(title_bar)

        # ── Output area ──
        self.output = QtWidgets.QTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(mono)
        self.output.setStyleSheet("""
            QTextEdit {
                background-color: #1a1a1a;
                color: #d4d4d4;
                border: 1px solid #3a3a3a;
                border-radius: 6px;
                padding: 8px;
            }
        """)
        root.addWidget(self.output, stretch=1)

        # ── Quick actions ──
        qa_label = QtWidgets.QLabel("Quick Actions:")
        qa_label.setFont(QtGui.QFont("Segoe UI", 9))
        qa_label.setStyleSheet("color: #888;")
        root.addWidget(qa_label)

        quick_grid = QtWidgets.QGridLayout()
        quick_grid.setSpacing(6)

        quick_actions = [
            ("📊 Script Stats",      "show script statistics"),
            ("⚠️  Missing Files",    "find all missing files"),
            ("🔴 Error Nodes",       "find nodes with errors"),
            ("⏸️  Disabled Nodes",   "list all disabled nodes"),
            ("🌳 Node Tree",         "show node tree"),
            ("⚡ Performance",       "analyze performance"),
            ("🐌 Bottlenecks",       "find bottlenecks"),
            ("✅ Validate Script",   "validate the entire script"),
        ]

        for i, (label, cmd) in enumerate(quick_actions):
            btn = QtWidgets.QPushButton(label)
            btn.setFont(QtGui.QFont("Segoe UI", 9))
            btn.setFixedHeight(28)
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #2d2d2d;
                    color: #ccc;
                    border: 1px solid #444;
                    border-radius: 4px;
                    padding: 0 8px;
                }
                QPushButton:hover {
                    background-color: #3a3a3a;
                    border-color: #e8c87a;
                    color: #e8c87a;
                }
                QPushButton:pressed { background-color: #252525; }
            """)
            btn.clicked.connect(lambda _checked=False, c=cmd: self._run_quick(c))
            quick_grid.addWidget(btn, i // 4, i % 4)

        root.addLayout(quick_grid)

        # ── Input row ──
        input_row = QtWidgets.QHBoxLayout()
        input_row.setSpacing(8)

        self.input_field = QtWidgets.QLineEdit()
        self.input_field.setFont(ui_font)
        self.input_field.setPlaceholderText("Ask anything… e.g. 'count all Read nodes', 'disable all Grade nodes'")
        self.input_field.setFixedHeight(36)
        self.input_field.setStyleSheet("""
            QLineEdit {
                background-color: #252525;
                color: #f0f0f0;
                border: 1px solid #444;
                border-radius: 6px;
                padding: 0 10px;
            }
            QLineEdit:focus { border-color: #e8c87a; }
        """)
        self.input_field.returnPressed.connect(self._on_run)

        self.run_btn = QtWidgets.QPushButton("▶  Run")
        self.run_btn.setFont(QtGui.QFont("Segoe UI", 10, QtGui.QFont.Bold))
        self.run_btn.setFixedSize(90, 36)
        self.run_btn.setStyleSheet("""
            QPushButton {
                background-color: #e8c87a;
                color: #1a1a1a;
                border: none;
                border-radius: 6px;
                font-weight: bold;
            }
            QPushButton:hover  { background-color: #f0d48a; }
            QPushButton:pressed { background-color: #c8a85a; }
            QPushButton:disabled { background-color: #555; color: #888; }
        """)
        self.run_btn.clicked.connect(lambda _: self._on_run())

        clear_btn = QtWidgets.QPushButton("🗑")
        clear_btn.setFixedSize(36, 36)
        clear_btn.setToolTip("Clear output")
        clear_btn.setStyleSheet("""
            QPushButton {
                background-color: #2d2d2d;
                color: #aaa;
                border: 1px solid #444;
                border-radius: 6px;
            }
            QPushButton:hover { background-color: #3a3a3a; color: #fff; }
        """)
        clear_btn.clicked.connect(lambda _: self.output.clear())

        input_row.addWidget(self.input_field, stretch=1)
        input_row.addWidget(self.run_btn)
        input_row.addWidget(clear_btn)
        root.addLayout(input_row)

        # ── Status bar ──
        self.status_label = QtWidgets.QLabel("Ready")
        self.status_label.setFont(QtGui.QFont("Segoe UI", 8))
        self.status_label.setStyleSheet("color: #666;")
        root.addWidget(self.status_label)

        # ── Window styling ──
        self.setStyleSheet("""
            QWidget {
                background-color: #121212;
                color: #d4d4d4;
            }
            QComboBox {
                background-color: #2d2d2d;
                color: #ccc;
                border: 1px solid #444;
                border-radius: 4px;
                padding: 2px 8px;
            }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView {
                background-color: #2d2d2d;
                color: #ccc;
                selection-background-color: #3a3a3a;
            }
        """)

    # ----------------------------------------------------------
    # UI LOGIC
    # ----------------------------------------------------------

    def _on_model_changed(self, model_name):
        self.ai.model = model_name
        self._log(f"ℹ️  Model switched to: {model_name}")

    def _run_quick(self, cmd):
        self.input_field.setText(cmd)
        self._on_run()

    def _on_run(self):
        prompt = self.input_field.text().strip()
        if not prompt:
            return

        self.input_field.clear()
        self._log(f"\n{'─'*55}")
        self._log(f"❓ {prompt}")
        self._log(f"{'─'*55}")

        self.run_btn.setEnabled(False)
        self.status_label.setText("Running…")

        # Run in background thread so GUI stays responsive
        self._worker = _AIWorker(self.ai, prompt)
        self._worker.log_signal.connect(self._append_to_output)
        self._worker.done_signal.connect(self._on_done)
        self._worker.start()

    def _on_done(self, result):
        self.run_btn.setEnabled(True)
        if result.get("success"):
            self.status_label.setText("✅ Done")
        else:
            self.status_label.setText("❌ Failed")

    def _log(self, msg):
        self.output.append(msg)
        self.output.verticalScrollBar().setValue(
            self.output.verticalScrollBar().maximum()
        )

    @QtCore.Slot(str)
    def _append_to_output(self, msg):
        self._log(msg)


class _AIWorker(QtCore.QThread):
    """Background thread for AI calls"""
    log_signal  = QtCore.Signal(str)
    done_signal = QtCore.Signal(dict)

    def __init__(self, ai, prompt):
        super().__init__()
        self.ai     = ai
        self.prompt = prompt

    def run(self):
        result = self.ai.ai_nuke(self.prompt, callback=self.log_signal.emit)
        self.done_signal.emit(result if result else {"success": False})


# ============================================================
# LAUNCH HELPERS
# ============================================================

_panel_instance = None

def show_nuke_ai_panel():
    """
    Call this from Nuke's Script Editor or menu to open the panel.
    """
    global _panel_instance
    if _panel_instance and not _panel_instance.isVisible():
        _panel_instance = None

    if _panel_instance is None:
        _panel_instance = NukeAIPanel()

    _panel_instance.show()
    _panel_instance.raise_()
    _panel_instance.activateWindow()
    return _panel_instance


def add_to_nuke_menu():
    """
    Add to menu.py:
        import nuke_ai_assistant
        nuke_ai_assistant.add_to_nuke_menu()
    """
    menubar = nuke.menu("Nuke")
    ai_menu = menubar.addMenu("AI Assistant")
    ai_menu.addCommand("Open AI Panel", show_nuke_ai_panel, "ctrl+shift+a")


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    if NUKE_ENV:
        # Running inside Nuke — show the panel
        show_nuke_ai_panel()
    else:
        # Running standalone — show GUI for testing layout
        import sys
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
        panel = NukeAIPanel()
        panel.show()
        sys.exit(app.exec_())
