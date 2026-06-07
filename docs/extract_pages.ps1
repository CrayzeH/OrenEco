
$ErrorActionPreference = 'Stop'
$folder = Join-Path $env:USERPROFILE 'Downloads\Telegram Desktop'
$file = Get-ChildItem -LiteralPath $folder -Filter '*.docx' | Where-Object { $_.Length -eq 18061675 } | Select-Object -First 1
$out = Join-Path (Resolve-Path '.').Path 'docs\paragraphs_pages.jsonl'
$word = New-Object -ComObject Word.Application
$word.Visible = $false
$doc = $word.Documents.Open($file.FullName, $false, $true)
$lines = New-Object System.Collections.Generic.List[string]
for ($i = 1; $i -le $doc.Paragraphs.Count; $i++) {
  $p = $doc.Paragraphs.Item($i)
  $text = $p.Range.Text -replace "\r", "" -replace "\a", "" -replace "\t", " "
  $text = $text.Trim()
  if ($text.Length -gt 0) {
    $page = $p.Range.Information(3)
    $obj = [ordered]@{ page=$page; paragraph=$i; text=$text }
    $lines.Add(($obj | ConvertTo-Json -Compress))
  }
}
[System.IO.File]::WriteAllLines($out, $lines, [System.Text.Encoding]::UTF8)
$doc.Close($false)
try { $word.Quit() } catch {}
