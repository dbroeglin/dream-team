import streamlit as st    
from autogen import Agent
from autogen.agentchat import UserProxyAgent, AssistantAgent, GroupChat, GroupChatManager  
from autogen.oai.openai_utils import config_list_from_json  
import warnings  

  
warnings.filterwarnings('ignore')  

# Callback function to print messages
# based on https://github.com/microsoft/autogen/issues/478
def print_messages_callback(recipient, messages, sender, config):     
    message = messages[-1]

    with st.expander(f"From {message['name']} to {recipient.name}", expanded=False):
        # st.write(f"From {sender} to {recipient}: ")
        st.write(message["content"])

    return False, None  # required to ensure the agent communication flow continues

# Configuration for GPT-4  
config_list_gpt4 = config_list_from_json(  
    "OAI_CONFIG_LIST.json",  
    filter_dict={  
        "model": ["gpt-4o"],  # gpt4o or gpt4-0409  
    },  
)  
gpt4_config = {  
    "cache_seed": 42,  # change the cache_seed for different trials  
    "temperature": 0,  
    "config_list": config_list_gpt4,  
    "timeout": 120,  
}  

if 'input_keys' not in st.session_state:
    st.session_state.input_keys= []
if 'saved_agents' not in st.session_state:
    st.session_state.saved_agents = []
if 'info' not in st.session_state:
    st.session_state.info = None
if 'able_to_run' not in st.session_state:
    st.session_state.able_to_run = False
if 'running' not in st.session_state:
    st.session_state.running = False
if 'saved_transitions' not in st.session_state:
    st.session_state.saved_transitions = {}
# Initialize session state for messages and first query flag  
if 'messages' not in st.session_state:  
    st.session_state.messages = []  
if 'first_query' not in st.session_state:  
    st.session_state.first_query = True  


# # TODO: REMOVE
# import json
# st.session_state.saved_agents = json.load(open("agents.json"))

info_placeholder = st.empty()

if not st.session_state.info:
    info_placeholder.empty()
else:
    info_placeholder.info(st.session_state.info)

if (st.session_state.saved_agents):
    # st.write("Agents loaded...")
    agents = st.session_state.saved_agents
    st.session_state.able_to_run = True
else:
    st.warning("No agents Created yet!")
    agents = []
    st.write("First, let's create a new agents.")
    st.page_link("pages/01_setup.py", icon="🤖")
    st.session_state.able_to_run = False

if (st.session_state.saved_transitions):
    st.session_state.able_to_run = True



with st.expander("Defined agents", expanded=False):
    # st.write("Defined Agents:")
    for val in agents:
        st.json(val)

with st.expander("Defined agents transifions", expanded=False):
    st.write(st.session_state.saved_transitions)

# TODO: add a check if agents are configured
def get_entry_agent(agents):
    # return agents[st.selectbox("Entry Agent", agents.keys())]
    return agents["Admin"]

def config(allowed_transitions, max_round=30, speaker_transitions_type="allowed"):
    st.session_state.info = "Configuring agents..."

    agents = {}
    for agent in st.session_state.saved_agents:
        if agent["type"] == "UserProxyAgent":
            if agent["human_input_mode"] == "ALWAYS":
                _a = UserProxyAgent(
                    name=agent["name"],
                    system_message=agent["system_message"],
                    description=agent["description"],
                    human_input_mode=agent["human_input_mode"],
                    code_execution_config=False,
                    is_termination_msg=lambda msg: "terminate" in msg.get("content").lower(),
                )
                _a.register_reply(
                    [Agent, None],
                    reply_func=print_messages_callback
                )
                _a.get_human_input = lambda prompt: "exit" # TODO: this is a hack to always exit the conversation
            elif agent["human_input_mode"] == "NEVER":
                _a = UserProxyAgent(
                    name=agent["name"],
                    system_message=agent["system_message"],
                    description=agent["description"],
                    human_input_mode=agent["human_input_mode"],
                    code_execution_config={  
                        "last_n_messages": 20,  
                        "work_dir": "dream",  
                        "use_docker": True,  
                    }
                )
                _a.register_reply(
                    [Agent, None],
                    reply_func=print_messages_callback
                )
        elif agent["type"] == "AssistantAgent":
            _a = AssistantAgent(
                name=agent["name"],
                system_message=agent["system_message"],
                description=agent["description"],
                llm_config=gpt4_config
            )
            _a.register_reply(
                [Agent, None],
                reply_func=print_messages_callback
            )
            
        else:
            st.error(f"Unknown agent type: {agent['type']}")
            st.stop()
        
        

        agents[agent["name"]] = _a

    for k, v in st.session_state.saved_transitions.items():
        allowed_transitions[agents[k]] = [agents[i] for i in v]

    entry_agent = get_entry_agent(agents)
    system_message_manager = "You are the manager of a research group your role is to manage the team and make sure the project is completed successfully."  

    groupchat = GroupChat(  
        agents=[v for _,v in agents.items()],
        allowed_or_disallowed_speaker_transitions=allowed_transitions,  
        speaker_transitions_type=speaker_transitions_type,  
        messages=[], 
        max_round=max_round,  
        send_introductions=True  
    ) 
    manager = GroupChatManager(groupchat=groupchat, llm_config=gpt4_config, system_message=system_message_manager)
    st.session_state.info = "Agents configured successfully!"
    
    return manager, entry_agent

def run(manager, user_proxy, task):
    st.session_state.info = "Running agents..."
    st.session_state.running = True
    
    if st.session_state.first_query: 
        chat_result = user_proxy.initiate_chat(manager, message=task, clear_history=True)  
        st.session_state.first_query = False  
    else:  
        chat_result = user_proxy.initiate_chat(manager, message=task, clear_history=False)  

    st.session_state.messages.append(chat_result)  
    # st.write("Chat Summary:")  
    with st.expander("Chat Summary", expanded=True):
        st.write(chat_result.summary)
    with st.expander("Chat History", expanded=False):
        st.write(chat_result.chat_history)

    st.session_state.running = False
    return chat_result
  
    # if st.button("Clear History"):  
    #     st.session_state.messages = []  
    #     st.session_state.first_query = True  
    #     st.write("Chat history cleared.")  
    
    # return None
    

if (st.session_state.able_to_run):

    task = st.text_input("Enter your task:", "What are the 10 leading GitHub repositories on llm for the legal domain?")  


    if st.button("Run Agents", type="primary"):
        
        # TODO: add a check if agents are configured
        manager, user_proxy = config(allowed_transitions={})

        with st.spinner("Running agents..."):
            chat_result = run(manager=manager, user_proxy=user_proxy, task=task)

        st.write("Done!")


