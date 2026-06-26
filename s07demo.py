"""
一个可以进行任务规划，解决用户问题的Agent
"""

import os # 提供系统环境变量，目录，文件的查改能力
import json, ast
from quopri import decodestring
import subprocess
from pathlib import Path
import yaml

from pydantic.type_adapter import P  # 启动子进程，执行系统命令

try:
    import readline # 增强终端输入体验，支持方向键编辑，历史命令，快捷键
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')

except ImportError:
    pass

from anthropic import Anthropic # Anthropic SDK
from dotenv import load_dotenv # 加载环境变量

load_dotenv(override=True)# 加载环境变量，主要是将.env的变量加载到系统环境变量中，以便可以通过os.getenv访问

# 使用自定义的base url，就删除掉官方默认的token，避免产生冲突
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
SKILLS_DIR = WORKDIR / "skills"
# 创建一个client，用于向LLM发送请求
client = Anthropic(base_url=os.getenv('ANTHROPIC_BASE_URL'))
# 读取模型名称
MODEL = os.environ["MODEL_ID"]
CURRENT_TODOS: list[dict] = []


# skill目录浏览
def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """将SKILL.md解析为YAML格式，返回(meta, body)"""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    return meta, parts[2].strip()

# skill字典，skill name到skill原数据dict的 map
SKILL_REGISTRY: dict[str, dict] = {}
def _scan_skills():
    f"""处理skills/ 目录，将每个skill转换为name, description, content的对象，保存到全局的skill字典当中"""
    if not SKILLS_DIR.exists():
        return 
    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir():
            continue
        manifest = d / "SKILL.md"
        if manifest.exists():
            raw = manifest.read_text()
            meta, body = _parse_frontmatter(raw) # 正文内容没用到
            name = meta.get("name", d.name)
            desc = meta.get("description", raw.split("\n")[0].lstrip("#").strip())
            SKILL_REGISTRY[name] = {"name": name, "description": desc, "content": raw}

_scan_skills()

def list_skills() -> str:
    """列出所有的skill name + description"""
    if not SKILL_REGISTRY:
        return "(no skills found)"
    return "\n".join(f"- **{s['name']}**: {s['description']}" for s in SKILL_REGISTRY.values())

def build_system() -> str:
    """生成系统提示词"""
    catalog = list_skills()
    return (
        f"你是一个位于{WORKDIR}的代码智能体。"
        f"可用的技能: \n{catalog}\n"
        "需要用skill的时候，使用load_skill去获取完整的细节"
    )
# 定义系统提示词
SYSTEM = build_system()

# 定义子Agent的系统提示词，不带任务规划的tool，防止无限递归
SUB_SYSTEM = (
    f"你是一个位于{WORKDIR}的代码智能体。"
    "完成分配给你的任务，返回简要的总结。"
    "不要进一步委派"
)

# 工具协议定义
TOOLS = [
    {
        "name": "bash",
        "description": "执行shell命令。",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        }
    },
    {
        "name": "read_file",
        "description": "读取文件内容",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "write_file",
        "description": "向文件写入内容",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "edit_file",
        "description": "替换文件中的确切文本一次",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"}
            },
            "required": ["path", "old_text", "new_text"]
        }
    },
    {
        "name": "glob",
        "description": "查找与glob模式匹配的文件",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
            },
            "required": ["pattern"]
        }
    },
    # plan tool
    {
        "name": "todo_write",
        "description": "为当前编码会话创建和管理任务列表",
        "input_schema": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}
                        },
                        "required": ["content", "status"]
                    }
                }
            },
            "required": ["todos"]
        }
    },
    {
        "name": "load_skill",
        "description": "通过名称加载完整的skill内容",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"]
        }
    }
]
# subagent tool 协议
SUB_TOOLS = [
        {
        "name": "bash",
        "description": "执行shell命令。",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        }
    },
    {
        "name": "read_file",
        "description": "读取文件内容",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "write_file",
        "description": "向文件写入内容",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "edit_file",
        "description": "替换文件中的确切文本一次",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"}
            },
            "required": ["path", "old_text", "new_text"]
        }
    },
    {
        "name": "glob",
        "description": "查找与glob模式匹配的文件",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
            },
            "required": ["pattern"]
        }
    },
]


# 具体工具实现
def run_bash(command: str) -> str:
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
        capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"

# 路径检查
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

# 新增4个新的工具
def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"...({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        file_path = safe_path(path) # 将字符串路径转换为绝对路径
        text = file_path.read_text() # 读取文件中的内容
        if old_text not in text:
            return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1)) # 将旧内容替换为新内容，并重新写入文件
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"

# 文件搜索
def run_glob(pattern: str) -> str:
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR):
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


# 校验todos数据，检查是否符合规范
def _normalize_todos(todos):
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except json.JSONDecodeError:
            try:
                todos = ast.literal_eval(todos)
            except (SyntaxError, ValueError):
                return None, "Error: todos must be a list or JSON array string"
    if not isinstance(todos, list):
        return None, "Error: todos must be a list"
    for i, t in enumerate(todos):
        if not isinstance(t, dict):
            return None, f"Error: todos[{i}] must be an object"
        if "content" not in t or "status" not in t:
            return None, f"Error: todos[{i} missing 'content' or 'status']"
        if t["status"] not in ("pending", "in_progress", "completed"):
            return None, f"Error: todos[{i} has invalid status '{t['status']}']"
    return todos, None

# 更新任务状态，打印状态
def run_todo_write(todos: list) -> str:
    global CURRENT_TODOS
    todos, error = _normalize_todos(todos)
    if error:
        return error    
    CURRENT_TODOS = todos
    lines = ["\n\033[33m## Current Tasks\033[0m"]
    for t in CURRENT_TODOS:
        icon = {"pending": " ", "in_progress": "\033[36m▸\033[0m", "completed": "\033[32m✓\033[0m"}[t["status"]]
        lines.append(f" [{icon}] {t['content']}")
    print("\n".join(lines))
    return f"Updated {len(CURRENT_TODOS)} tasks"

def load_skill(name: str) -> str:
    """加载完整的skill内容。通过注册表查询，无需遍历路径"""
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        return f"Skill not found: {name}"
    return skill["content"]

# 定义字符串到函数的映射map TOOL_HANDLERS
TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "todo_write": run_todo_write,
    "load_skill":load_skill,
}
# 定义子Agent 工具名称到工具函数的映射map，SUB_HANDLERS
SUB_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
}

def extract_text(content) -> str:
    """从上下文中的content blocks中提取text"""
    if not isinstance(content, list):
        return str(content)
    return "\n".join(getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text")

def spawn_subagent(description: str) -> str:
    """创建一个具有干净上下文的子agent，仅仅返回总结"""
    print(f"\n\033[35m[子agent 创建]\033[0m")
    messages = [{"role": "user", "content": description}]

    for _ in range(30):  # 安全限制 30
        response = client.messages.create(
            model=MODEL, system=SUB_SYSTEM,
            messages=messages, tools=SUB_TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            break
        results =[]
        for block in response.content:
            if block.type == "tool_use":
                blocked = trigger_hooks("PreToolUse", block)
                if blocked:
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": str(blocked)})
                    continue
                handler = SUB_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown: {block.name}"
                trigger_hooks("PostToolUse", block, output)
                print(f"    \033[90m[sub] {block.name}: {str(output)[:100]}\033[0m")
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": output})
        messages.append({"role": "user", "content": results})
    
    # 提取子agent的结果
    result = extract_text(messages[-1]["content"])
    if not result:
        # 最后一次是tool_result, 去找最后一次大模型输出的block
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                result = extract_text(msg["content"])
                if result:
                    break
        if not result:
            result = "Subagent stopped after 30 turns without final answer."
    print(f"\033[35m[Subagent done]\033[0m")
    return result

TOOLS.append({
    "name": "task",
    "description": "派发一个子Agent去处理复杂的子任务。仅仅返回最后的结论。",
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {"type": "string"}
        },
        "required": ["description"]
    }
})
TOOL_HANDLERS["task"] = spawn_subagent

# 钩子系统 意义在于把Agent loop之外的机制（日志，权限检查，生命周期）解耦出来，变成可插拔的hook层，这样在后续增加日志或者埋点等操作时，更方便扩展，避免写死在loop里，变成胶水代码
HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}

def register_hook(event: str, callback):
    HOOKS[event].append(callback)

def trigger_hooks(event: str, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None: # 注册的hook如果返回非None，则终止hook
            return result
    return None  # 

# 把权限检查封装成一个hook
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/sda"]
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]

def permission_hook(block):
    """PreToolUse"""
    if block.name == "bash":
        for pattern in DENY_LIST:
            if pattern in block.input.get("command", ""):
                print(f"\n\033[31m⛔ Blocked: '{pattern}'\033[0m")
                return "Permission denied by deny list"
        for kw in DESTRUCTIVE:
            if kw in block.input.get("command", ""):
                print(f"\n\033[33m⚠ 潜在的危险命令\033[0m")
                print(f"    Tool: {block.name}({block.input})")
                choice = input("    Allow? [y/N] ").strip().lower()
                if choice not in ("y", "yes"):
                    return "Permission denied by user"
    if block.name in ("write_file", "edit_file"):
        path = block.input.get("path", "")
        if not (WORKDIR / path).resolve().is_relative_to(WORKDIR):
            print(f"\n\033[33m⚠  Writing outside workspace\033[0m")
            print(f"    Tool: {block.name}({block.input})")
            choice = input("    Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by users"
    return None 

# log_hook
def log_hook(block):
    """PreToolUse: 打印每一次tool call"""
    args_preview = str(list(block.input.values())[:2])[:60]
    print(f"\033[90m[HOOK] {block.name}({args_preview})\033[0m")
    return None

# large output hook
def large_output_hook(block, output):
    """PostToolUse: 输出过长"""
    if len(str(output)) > 100000:
        print(f"\033[33m[HOOK] ⚠ 输出太大了 {block.name}: {len(str(output))} 字符\033[0m")
    return None

# UserPromptSubmit hook: 在用户的输入进入大模型之前打印一下日志
def context_inject_hook(query: str):
    """"UserPromptSubmit hook"""
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None

# Stop hook: Agent loop结束之后打印一下总结，工具调用的次数
def summary_hook(messages: list):
    tool_count = sum(1 for m in messages
                     for b in (m.get("content") if isinstance(m.get("content"), list) else [])
                     if isinstance(b, dict) and b.get("type") == "tool_result")
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None 
  
register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)

## Agent loop
rounds_since_todo = 0
def agent_loop(messages: list):
    global rounds_since_todo
    while True:

        # 三次response都没有todo_write tool call的时候，在上下文中注入提醒
        if rounds_since_todo >=3 and messages:
            messages.append({"role": "user",
                            "content": "<reminder> 更新你的todos.</reminder>"})
            rounds_since_todo = 0
        # 这里是按照anthropic 服务端定义的接口协议返回的结构，并不是大模型直接的输出    因为在sdk里没有找到实现function call的提示词（想知道他们是怎么让LLM按照固定格式返回内容？返回的内容出错是怎么处理的？）
        response = client.messages.create(model=MODEL, messages=messages, max_tokens=8000,system=SYSTEM, tools=TOOLS)
        # 保存到历史对话中
        messages.append({"role": "assistant", "content": response.content})
        # 如果没有工具调用，直接返回
        if response.stop_reason != 'tool_use':
            force = trigger_hooks("Stop", messages)  # Stop hook
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return 
        
        rounds_since_todo += 1
        results = []
        for block in response.content:
            # 执行所有的tool_use，并保存结果
            if block.type == 'tool_use':
                blocked = trigger_hooks("PreToolUse", block) # Pre hook
                if blocked:
                    results.append({"type": "tool_result", "tool_use_id": block.id, 
                                    "content": str(blocked)})
                    continue
                handler = TOOL_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unkown: {block.name}"

                trigger_hooks("PostToolUse", block, output) # post hook
                if block.name == "todo_write":
                    rounds_since_todo = 0
                # 保存到results中
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output
                })
        # 将tool use执行结果保存到历史对话中
        # tool_result消息 必须紧跟在对应的tool_use消息后边，中间不能有其他的消息 参考资料：https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls#handling-results-from-client-tools
        messages.append({"role": "user", "content": results})       

# 从终端读取输入
if __name__ == "__main__":
    print("s07: Skill加载 - 在系统提示词中加入skill的大纲，具体skill内容按需加载")
    print("输入问题，回车发送。输入q退出. \n")
    
    history = []
    while True:
        try: 
            query = input("\033[36ms01 >> \033[0m") # 从终端读取用户输入，\033代表ESC，代表ANSI终端控制码的开头，[36m代表设置终端内字符为青色，[0m
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        trigger_hooks("UserPromptSubmit", query)  # UserPrompt hook
        history.append({"role": "user", "content": query})
        agent_loop(history)
        # 打印模型最后一次回复的内容
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if getattr(block, 'type', None) == 'text':
                    print(block.text)

        print()
    # print(run_edit('hello.py', '你就是个大聪明', 'import torch\n print(torch.cuda.is_avaliable)')) 工具测试
    # 测试工具可用性
    # outputs = run_bash('pwd')
    # print(f"output; {outputs}")