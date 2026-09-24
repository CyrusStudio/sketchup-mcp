# Startup script for a scripted SketchUp launch:
#   SketchUp.exe -RubyStartup <this file>
#
# It loads the extension, starts the MCP server and writes a status JSON so a
# test runner can tell, without guessing, whether the server is actually up.
# Override the report location with SU_MCP_STATUS_FILE.
require 'sketchup.rb'
require 'json'

SU_MCP_STATUS_FILE =
  ENV['SU_MCP_STATUS_FILE'] || File.join(ENV['TEMP'].to_s.tr('\\', '/'), 'su_mcp_startup.json')

UI.start_timer(1, false) do
  status = { 'reported_at' => Time.now.to_s }
  begin
    require 'su_mcp/main'
    server = SU_MCP.server || SU_MCP.instance_variable_get(:@server)
    status['loaded'] = true
    status['started'] = server.start
    status['running'] = server.running?
    status['host'] = server.host
    status['port'] = server.port
    model = Sketchup.active_model
    status['sketchup'] = Sketchup.version
    status['ruby'] = RUBY_VERSION
    status['model_path'] = model ? model.path : nil
    status['model_modified'] = model ? model.modified? : nil
    status['model_entities'] = model ? model.entities.length : nil
  rescue Exception => e
    status['error'] = "#{e.class}: #{e.message}"
    status['backtrace'] = (e.backtrace || []).first(20)
  end

  begin
    dir = File.dirname(SU_MCP_STATUS_FILE)
    require 'fileutils'
    FileUtils.mkdir_p(dir) unless File.directory?(dir)
    File.open(SU_MCP_STATUS_FILE, 'w') { |f| f.write(JSON.pretty_generate(status)) }
  rescue Exception => e
    puts "MCP startup: could not write #{SU_MCP_STATUS_FILE}: #{e.message}"
  end
end
