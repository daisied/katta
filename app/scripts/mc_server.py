import os
import subprocess

# mc server script
mc_dir = '/app/minecraft'
server_jar = os.path.join(mc_dir, 'minecraft_server.1.21.jar')
eula_file = os.path.join(mc_dir, 'eula.txt')
server_props = os.path.join(mc_dir, 'server.properties')

# ensure dir and jar
os.makedirs(mc_dir, exist_ok=True)
if not os.path.exists(server_jar):
    print("downloading jar...")
    subprocess.run(['wget', 'https://piston-data.mojang.com/v1/objects/84194a2f286ef7c14ed7ce0090dba59902951553/server.jar', '-O', server_jar], check=True)

# accept eula
with open(eula_file, 'w') as f:
    f.write('eula=true\n')

# basic server.properties (online-mode=false, op for MareK99)
props = """#Minecraft server properties
online-mode=false
server-port=25565
motd=Public Test Server
difficulty=normal
gamemode=survival
pvp=true
max-players=20
server-ip=
level-type=minecraft:flat
enable-rcon=false
"""

with open(server_props, 'w') as f:
    f.write(props)

# op MareK99
ops_file = os.path.join(mc_dir, 'ops.json')
ops = [{"uuid": "00000000-0000-0000-0000-000000000000", "name": "MareK99", "level": 4, "bypassesPlayerLimit": False}]
with open(ops_file, 'w') as f:
    import json
    json.dump(ops, f)

print("starting mc server... (pid will print, ctrl+c to stop)")
print("listen on all ips:25565")

cmd = ['java', '-Xms2G', '-Xmx4G', '-jar', server_jar, 'nogui']
proc = subprocess.Popen(cmd, cwd=mc_dir)

try:
    proc.wait()
except KeyboardInterrupt:
    print("\nstopping...")
    proc.terminate()
    proc.wait()
