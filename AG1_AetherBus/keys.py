# keys.py
class StreamKeyBuilder:
    def __init__(self, namespace="AG1"):
        self.ns = namespace

    def flow_input(self, flow_id):
        return f"{self.ns}:flow:{flow_id}:input"

    def flow_output(self, flow_id):
        return f"{self.ns}:flow:{flow_id}:output"

    def agent_outbox(self, agent_id):
        return f"{self.ns}:agent:{agent_id}:outbox"

    def user_inbox(self, user_id):
        return f"{self.ns}:user:{user_id}:inbox"

    def agent_inbox(self, agent_id):
        return f"{self.ns}:agent:{agent_id}:inbox"

    def session_stream(self, session_code):
        return f"{self.ns}:session:{session_code}:stream"

    def edge_register(self, platform):
        return f"{self.ns}:edge:{platform}:register"

    def edge_stream(self, platform, target):
        return f"{self.ns}:edge:{platform}:{target}:stream"

    def edge_response(self, platform, target):
        return f"{self.ns}:edge:{platform}:{target}:response"

    def billing_ledger(self, agent_id):
        return f"{self.ns}:billing:{agent_id}:ledger"

    def memory_key(self, cassette_id):
        return f"{self.ns}:memory:{cassette_id}:write"

    def ans_key(self, agent_id):
        return f"ANS:{agent_id}"  # Not namespaced to AG1