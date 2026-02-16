import importlib.util
import inspect
import logging
import os
import sys

logger = logging.getLogger(__name__)

class PluginManager:
    def __init__(self, plugin_dir="/app/app/plugins"):
        self.plugin_dir = plugin_dir
        # Ensure plugin dir exists
        if not os.path.exists(self.plugin_dir):
            os.makedirs(self.plugin_dir, exist_ok=True)
            # Create a dummy readme
            with open(os.path.join(self.plugin_dir, 'README.md'), 'w') as f:
                f.write("# Plugins Directory\nDrop python scripts here. They will be loaded as tools.")
        
        self.plugins = {} # Name -> Callable

    def reload_plugins(self):
        """Scans the plugin directory and loads all python scripts."""
        logger.info(f"Reloading plugins from {self.plugin_dir}")
        self.plugins = {}
        
        if not os.path.exists(self.plugin_dir):
            return

        for filename in os.listdir(self.plugin_dir):
            if filename.endswith(".py") and not filename.startswith("_"):
                self._load_plugin_file(filename)

        logger.info(f"Loaded {len(self.plugins)} plugins: {list(self.plugins.keys())}")

    def _load_plugin_file(self, filename):
        file_path = os.path.join(self.plugin_dir, filename)
        module_name = filename[:-3] # strip .py
        
        try:
            spec = importlib.util.spec_from_file_location(module_name, file_path)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                
                # Scan for functions in the module
                for name, obj in inspect.getmembers(module):
                    if inspect.isfunction(obj) and not name.startswith("_"):
                        # We assume every public function in a plugin file is a tool
                        # In a more advanced system, we might look for a @tool decorator
                        # For now, to keep it "utilitarian", we treat all functions as tools.
                        # Naming convention: {module_name}_{function_name} or just {function_name}
                        # Let's use function name to keep it simple for the LLM, 
                        # but warn about collisions.
                        
                        tool_name = name
                        self.plugins[tool_name] = obj
                        logger.debug(f"Registered plugin tool: {tool_name}")
                        
        except Exception as e:
            logger.error(f"Failed to load plugin {filename}: {e}")

    def get_tool_definitions(self):
        """Returns OpenAI-compatible tool definitions for all loaded plugins."""
        definitions = []
        for name, func in self.plugins.items():
            # Basic introspection to generate schema
            # This is a simplified generator. 
            # Ideally use pydantic or strict docstring parsing.
            doc = func.__doc__ or "No description provided."
            
            # Simple parameter extraction (very basic)
            # For robust production use, we'd need full type hint parsing.
            params = {"type": "object", "properties": {}, "required": []}
            sig = inspect.signature(func)
            for param_name, param in sig.parameters.items():
                params["properties"][param_name] = {"type": "string", "description": "Parameter"}
                if param.default == inspect.Parameter.empty:
                    params["required"].append(param_name)
            
            definitions.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": doc,
                    "parameters": params
                }
            })
        return definitions

    def get_tool_callable(self, tool_name):
        return self.plugins.get(tool_name)
