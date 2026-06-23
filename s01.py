# agent loop 
# 1.历史会话,每一条消息是一个对象。用整个数组保存所有的会话。
from re import M


messages = [{"role": "user", "content": query}]

# 2.把消息和工具定义传入大模型
response = client.message.create(
    model,system,message,tools,max_tokens=8000,
)

# 3.判断模型的回复是否包含工具调用，如果不包含工具调用，结束对话。
messages.append({"role": "assistant", "content": response.content})
if response.stop_reason != 'tool_use':
    return

# 4.如果包含工具调用，执行调用并保存结果到历史结果中
results = []
for block in response.content:
    if block.type == 'tool_use':
        output = run_bash((block.input['command']))
        results.append({
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": output
        })

# 5.把工具调用的结果保存到历史会话中，然后执行第2步
messages.append({"role": "user", "content": results})

# 6.整合起来，一个任务的loop就是
def agent_loop():
    query = "用户的任务或者提问" #
    messages = [{"role": "user", "content": query}]
    while true:
        response = client.message.create(
            model=MODEL,
            system=SYSTEM,
            tools=TOOLS,
            max_tokens=8000,
            messages=MESSAGES
        )
        # 保存返回的内容到历史会话中
        messages.append({"role": "assistant", "content": response.content})
        # {stop_reson, content: [{type, id, input},{},{}]}
        if response.stop_reason != 'tool_use':
            return
        results = []
        for block in response.content:
            if (block.type == 'tool_use'):
                output = run_bash((block.input['command']))
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output
                })
        messages.append({"role": "user", "content": results})

