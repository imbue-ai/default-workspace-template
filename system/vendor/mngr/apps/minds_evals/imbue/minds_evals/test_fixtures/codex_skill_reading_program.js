const a = await tools.shell_command({"command":"cat .agents/skills/build-app/SKILL.md","workdir":"/home/user/workspace"});
const b = await tools.exec_command({cmd:`sed -n '1,80p' .agents/skills/frontend-design/SKILL.md`,workdir:"/home/user/workspace"});
const c = await tools.shell_command({"command":"wc -l .agents/skills/build-app/SKILL.md"});
