"""
一个可以用bash命令解决用户问题的Agent
"""

import os # 提供系统环境变量，目录，文件的查改能力
import subprocess
from pathlib import Path

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
# 创建一个client，用于向LLM发送请求
client = Anthropic(base_url=os.getenv('ANTHROPIC_BASE_URL'))
# 读取模型名称
MODEL = os.environ["MODEL_ID"]

# 定义系统提示词
SYSTEM = f"你是一个位于{WORKDIR}的代码智能体。使用bash去解决任务。做，不要解释。"

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
    }
    
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

# 定义字符串到函数的映射map TOOL_HANDLERS
TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
}

DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/sda"]
# bash门控检查1，直接禁止执行
def check_deny_list(command: str) -> str | None:
    for pattern in DENY_LIST:
        if pattern in command:
            return f"Blocked: '{pattern}' is on the deny list"
    return None

# 门控检查2，规则匹配
PERMISSION_RULES = [
    {
        "tools": ["write_file", "edit_file"],
        "check": lambda args: not (WORKDIR / args.get("path", "").resolve().is_relative_to(WORKDIR)),
        "message": "操作到工作空间之外了"
    },

    {
        "tools": ["bash"],
        "check": lambda args: any(kw in args.get("command", "") for kw in ["rm ", "> /etc/", "chmod 777"]),
        "message": "潜在毁灭性的命令",
    }
]

def check_rules(tool_name: str, args: dict) -> str | None:
    for rule in PERMISSION_RULES:
        if tool_name in rule["tools"] and rule["check"](args):
            return rule["message"]
    return None

# 门控检查3，由用户决定是否执行 
def ask_user(tool_name: str, args: dict, reason: str) -> str:
    print(f"\n\033[33m⚠ {reason}\033[0m")
    print(f"    Tool: {tool_name}{args}")
    choice = input("    Allow? [y/N] ").strip().lower()
    return "allow" if choice in ("y", "yes") else "deny"

# 整个安全检查流程
def check_permission(block) -> bool:
    if block.name == "bash":
        reason = check_deny_list(block.input.get("command", ""))
        if reason:
            print(f"\n\033[31m⛔ {reason}\033[0m")
            return False
    reason = check_rules(block.name, block.input)
    if reason:
        decision = ask_user(block.name, block.input, reason)
        if decision == "deny":
            return False
    return True

## Agent loop
def agent_loop(messages: list):
    while True:
        # 这里是按照anthropic 服务端定义的接口协议返回的结构，并不是大模型直接的输出    因为在sdk里没有找到实现function call的提示词（想知道他们是怎么让LLM按照固定格式返回内容？返回的内容出错是怎么处理的？）
        response = client.messages.create(model=MODEL, messages=messages, max_tokens=8000,system=SYSTEM, tools=TOOLS)
        # 保存到历史对话中
        messages.append({"role": "assistant", "content": response.content})
        # 如果没有工具调用，直接返回
        if response.stop_reason != 'tool_use':
            return
        results = []
        for block in response.content:
            # 执行所有的tool_use，并保存结果
            if block.type == 'tool_use':
                print(f"\033[33m> {block.name}\033[0m")
                if not check_permission(block):
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": "Permission denied."})
                    continue
                handler = TOOL_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unkown: {block.name}"
                print(f"工具执行结果：{output}")
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
    print("s03: 权限检查")
    print("输入问题，回车发送。输入q退出. \n")
    
    history = []
    while True:
        try: 
            query = input("\033[36ms01 >> \033[0m") # 从终端读取用户输入，\033代表ESC，代表ANSI终端控制码的开头，[36m代表设置终端内字符为青色，[0m
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
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