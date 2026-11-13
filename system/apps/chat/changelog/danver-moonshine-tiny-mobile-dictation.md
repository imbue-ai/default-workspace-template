Dictation on a phone or tablet (a page whose primary pointer is a finger) runs Moonshine Tiny, the lighter speech model, in place of Moonshine Base: a third of the download and under half the time per utterance, so the words keep up with the speaker. A desktop keeps Moonshine Base. A browser's `workspace-voice:engine` override still picks any registered engine on either.

On a phone or tablet the model also runs on WebAssembly's quantized weights even where the browser has WebGPU, so the page keeps a third of the memory the full-precision weights would take.
