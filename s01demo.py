"""
一个可以用bash命令解决用户问题的Agent
"""

import os # 提供系统环境变量，目录，文件的查改能力
import subprocess  # 启动子进程，执行系统命令

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

# 创建一个client，用于向LLM发送请求
client = Anthropic(base_url=os.getenv('ANTHROPIC_BASE_URL'))
# 读取模型名称
MODEL = os.environ["MODEL_ID"]

# 定义系统提示词
SYSTEM = f"你是一个位于{os.getcwd()}的代码智能体。使用bash去解决任务。做，不要解释。"

# 工具定义
TOOLS = [
    {
        "name": "bash",
        "description": "执行shell命令。",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        }
    }
]

# 工具执行
def run_bash(command: str) -> str:
    # 定义命令的边界，如果出下以下命令，不允许执行，做安全检查
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]

    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=os.getcwd(),
        capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: "

# 测试工具可用性
# outputs = run_bash('pwd')
# print(f"output; {outputs}")

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
                # 执行bash脚本并保存
                output = run_bash(block.input['command'])
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
    print("s01: 智能体循环")
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

