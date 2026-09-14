# NQX 音声キューの素材づくり —— **オフラインの予備手段**(R65, 2026-09-07)。
#
# 本番の音源は neural.py(Edge のニューラル TTS = 実在の男性の声)で作る。この PC の SAPI には
# 英語は女性(Zira)しか入っておらず、女性声を下げて男性にする試みは 2 度失敗した。
# それでもネットに出られない環境で作り直す必要が出たときのために、SAPI 経路を残しておく。
#
#   powershell -File toolsoice\synth.ps1 -OutDir <生WAVの置き場>
#   python toolsoiceoicefx.py --raw <生WAVの置き場> --out publicoice
#
# 台本の正本は lines.json(sound.js の CUE_TEXT と一致することをテストで縛る)。
# ここでは声の加工をしない。生 WAV は出荷しない。
#
# ※ PowerShell 5.1 は BOM 無し UTF-8 を ANSI として読む。このファイルは BOM 付きで保存すること
#   (無いと日本語コメントが壊れ、param ブロックごと構文エラーになる)。
param(
  [Parameter(Mandatory = $true)][string]$OutDir,
  [string]$VoiceName = "",
  [int]$Rate = 0
)

Add-Type -AssemblyName System.Speech

$linesPath = Join-Path $PSScriptRoot "lines.json"
$table = Get-Content $linesPath -Raw -Encoding UTF8 | ConvertFrom-Json

if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Force $OutDir | Out-Null }

$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
if ($VoiceName -ne "") { $synth.SelectVoice($VoiceName) }
$synth.Rate = $Rate
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,
  [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
  [System.Speech.AudioFormat.AudioChannel]::Mono)

foreach ($prop in $table.PSObject.Properties) {
  if ($prop.Name.StartsWith("_")) { continue }
  $path = Join-Path $OutDir ($prop.Name + ".wav")
  $synth.SetOutputToWaveFile($path, $fmt)
  $synth.Speak($prop.Value)
  $synth.SetOutputToNull()
  Write-Output ("{0} <- {1}" -f $path, $prop.Value)
}
$synth.Dispose()
