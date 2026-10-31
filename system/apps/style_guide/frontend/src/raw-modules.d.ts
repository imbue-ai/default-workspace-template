/** A stylesheet imported as its source text (Vite's `?raw` suffix), which the catalog reads its tokens out of. */
declare module "*.css?raw" {
  const source: string;
  export default source;
}
