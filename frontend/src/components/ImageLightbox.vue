<script setup>
import { watch } from 'vue'

const props = defineProps({
  src: { type: String, required: true },
  alt: { type: String, default: '' },
  visible: { type: Boolean, default: false },
})

const emit = defineEmits(['close'])

function onKeydown(e) {
  if (e.key === 'Escape') emit('close')
}
</script>

<template>
  <Teleport to="body">
    <div
      v-if="visible"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/90 cursor-zoom-out"
      @click="emit('close')"
      @keydown="onKeydown"
      tabindex="0"
    >
      <!-- Close button -->
      <button
        @click="emit('close')"
        class="absolute top-4 right-4 text-white/70 hover:text-white text-3xl w-10 h-10 rounded-full
               bg-white/10 hover:bg-white/20 flex items-center justify-center z-10 transition-colors"
      >
        &times;
      </button>

      <!-- Image -->
      <img
        :src="src"
        :alt="alt"
        class="max-w-[95vw] max-h-[95vh] object-contain rounded-lg shadow-2xl"
        @click.stop
      />

      <!-- Alt text -->
      <div v-if="alt" class="absolute bottom-4 left-1/2 -translate-x-1/2 bg-black/60 text-white/80
                      text-sm px-4 py-2 rounded-lg max-w-[80vw] truncate">
        {{ alt }}
      </div>
    </div>
  </Teleport>
</template>
