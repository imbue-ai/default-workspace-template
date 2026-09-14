# Frontend choices

Use these default choices unless the user has requested something different, or the specific task clearly requires a different choice.

## CSS: Graffity
`assets/drop-in.css` in this skill contains the CSS file you should use. It comes from the Graffiti project. Please copy this into your frontend and use it as your main (and oftentimes only) CSS.

You can modify your copy if necessary to fit the needs of your app or a user request.

Try to use the components already provided in drop-in.css whenever possible.

The CSS includes font & color choices, as well as stylings for all the default HTML elements.

## Theme selection
Use this on your HTML tag to select the default theme:
```html
<html class="theme-schematic">...</html>
```
Select a different theme only if the user request requires it.