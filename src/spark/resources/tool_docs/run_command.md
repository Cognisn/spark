# Tool: run_command

## Purpose

Execute shell commands on the host system. The tool is OS-aware, automatically selecting the appropriate shell for the platform — `zsh` on macOS, `bash` on Linux, and `cmd` on Windows. Use for running CLI tools, scripts, git operations, system diagnostics, and any other command-line task.

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| command | string | Yes | - | The shell command to execute |
| working_directory | string | No | User's home directory | Directory to run the command in |
| timeout | integer | No | 30 | Maximum execution time in seconds |

## Return Value

```json
{
    "exit_code": 0,
    "output": "combined stdout and stderr output here"
}
```

Non-zero exit codes indicate the command failed:
```json
{
    "exit_code": 1,
    "output": "ls: cannot access '/nonexistent': No such file or directory"
}
```

Output that exceeds the configured limit is truncated with an indication that content was omitted.

## Examples

### Git Operations

```json
{
    "tool": "run_command",
    "input": {
        "command": "git status",
        "working_directory": "/Users/matt/projects/my-app"
    }
}
```

```json
{
    "tool": "run_command",
    "input": {
        "command": "git log --oneline -10",
        "working_directory": "/Users/matt/projects/my-app"
    }
}
```

### Docker Commands

```json
{
    "tool": "run_command",
    "input": {
        "command": "docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'"
    }
}
```

```json
{
    "tool": "run_command",
    "input": {
        "command": "docker logs --tail 50 my-container"
    }
}
```

### AWS CLI

```json
{
    "tool": "run_command",
    "input": {
        "command": "aws s3 ls s3://my-bucket/ --human-readable"
    }
}
```

```json
{
    "tool": "run_command",
    "input": {
        "command": "aws sts get-caller-identity"
    }
}
```

### File Operations

```json
{
    "tool": "run_command",
    "input": {
        "command": "ls -la /var/log/",
        "working_directory": "/"
    }
}
```

```json
{
    "tool": "run_command",
    "input": {
        "command": "grep -r 'ERROR' /var/log/app/ --include='*.log' | tail -20"
    }
}
```

```json
{
    "tool": "run_command",
    "input": {
        "command": "find . -name '*.py' -mtime -7",
        "working_directory": "/Users/matt/projects/my-app"
    }
}
```

### Network Diagnostics

```json
{
    "tool": "run_command",
    "input": {
        "command": "curl -s -o /dev/null -w '%{http_code}' https://api.example.com/health"
    }
}
```

```json
{
    "tool": "run_command",
    "input": {
        "command": "ping -c 4 google.com",
        "timeout": 15
    }
}
```

```json
{
    "tool": "run_command",
    "input": {
        "command": "dig example.com A +short"
    }
}
```

### System Information

```json
{
    "tool": "run_command",
    "input": {
        "command": "uname -a"
    }
}
```

```json
{
    "tool": "run_command",
    "input": {
        "command": "df -h"
    }
}
```

```json
{
    "tool": "run_command",
    "input": {
        "command": "uptime"
    }
}
```

### Package Managers

```json
{
    "tool": "run_command",
    "input": {
        "command": "brew list --formula"
    }
}
```

```json
{
    "tool": "run_command",
    "input": {
        "command": "pip list --outdated"
    }
}
```

## Platform-Specific Examples

### macOS

```json
{
    "tool": "run_command",
    "input": {
        "command": "open https://github.com"
    }
}
```

```json
{
    "tool": "run_command",
    "input": {
        "command": "echo 'Hello' | pbcopy"
    }
}
```

### Linux

```json
{
    "tool": "run_command",
    "input": {
        "command": "xdg-open https://github.com"
    }
}
```

### Windows

```json
{
    "tool": "run_command",
    "input": {
        "command": "dir /s /b *.txt"
    }
}
```

```json
{
    "tool": "run_command",
    "input": {
        "command": "systeminfo | findstr /B /C:\"OS\""
    }
}
```

## Security Considerations

| Concern | Detail |
|---------|--------|
| Blocked commands | Dangerous commands (e.g. `rm -rf /`, `mkfs`, `dd if=/dev/zero`) are blocked |
| Timeout limits | Commands exceeding the timeout are terminated automatically |
| User context | Commands run as the same user that launched Spark — no privilege escalation |
| No shell injection | Commands are executed directly; however, exercise caution with user-supplied input |
| Approval required | This is a mutation tool — each invocation requires explicit user approval |

## Best Practices

- Check that a command exists before running it: `which docker` or `command -v aws`
- Use absolute paths for files and executables to avoid ambiguity
- Pipe output through `head`, `tail`, or `grep` to limit result size
- Set an appropriate `timeout` for long-running commands (e.g. network requests, large builds)
- Use `working_directory` rather than `cd` in the command string
- Handle errors by checking the `exit_code` in the response
- Combine commands with `&&` for sequential operations that depend on each other

## Common Pitfalls

- **Interactive commands**: Tools like `vi`, `less`, `nano`, or `top` require a TTY and will not work
- **Commands needing TTY input**: `sudo` with a password prompt, `ssh` without key-based auth, or any command expecting user interaction will hang until timeout
- **Background processes**: Commands using `&` to background a process will return immediately; the process may be orphaned when the shell exits
- **Very large output**: Commands producing enormous output (e.g. `cat` on a multi-gigabyte file) will be truncated and may consume excessive memory
- **Environment variables**: The shell environment may differ from your interactive terminal — explicitly set variables if needed
- **Relative paths**: Without `working_directory`, commands run in the user's home directory, which may not be the intended location

## Related Tools

- `write_file` — Write content to a file on disk
- `read_file` — Read text file contents
- `list_directory` — List files and subdirectories
