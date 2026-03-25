param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

python "$PSScriptRoot\standalone.py" run @Args
