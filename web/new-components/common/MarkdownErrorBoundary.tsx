import React from 'react';

interface Props {
  text: string;
  errorLabel: string;
  retryLabel: string;
  children: React.ReactNode;
}

export default class MarkdownErrorBoundary extends React.Component<Props, { hasError: boolean }> {
  state = { hasError: false };

  static getDerivedStateFromError() {
    return { hasError: true };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error('Markdown rendering failed', { name: error.name, componentStack: info.componentStack });
  }

  render() {
    if (!this.state.hasError) return this.props.children;

    return (
      <div>
        <div role='alert' className='mb-2 text-sm text-amber-700 dark:text-amber-400'>
          {this.props.errorLabel}
        </div>
        <div className='whitespace-pre-wrap break-words'>{this.props.text}</div>
        <button
          type='button'
          className='mt-2 rounded border border-gray-300 px-3 py-1 text-sm dark:border-gray-600'
          onClick={() => this.setState({ hasError: false })}
        >
          {this.props.retryLabel}
        </button>
      </div>
    );
  }
}
